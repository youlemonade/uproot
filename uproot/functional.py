#!/usr/bin/env python

# Copyright (c) 2017, DIANA-HEP
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import ast
import keyword
import math
import sys
from collections import namedtuple

import numpy

import uproot.tree
import uproot.interp.auto

def ifinstalled(f):
    try:
        import numba
    except ImportError:
        return f
    else:
        return numba.njit()(f)

if sys.version_info[0] <= 2:
    parsable = (unicode, str)
else:
    parsable = (str,)

def makefcn(code, env, name):
    env = dict(env)
    exec(code, env)
    return env[name]

def string2fcn(string):
    insymbols = set()
    outsymbols = set()

    def recurse(node):
        if isinstance(node, ast.FunctionDef):
            raise TypeError("function definitions are not allowed in a function parsed from a string")

        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if node.id not in outsymbols:
                    insymbols.add(node.id)
            elif isinstance(node.ctx, ast.Store):
                outsymbols.add(node.id)

        elif isinstance(node, ast.AST):
            for field in node._fields:
                recurse(getattr(node, field))

        elif isinstance(node, list):
            for x in node:
                recurse(x)
        
    body = ast.parse(string).body
    recurse(body)

    if len(body) == 0:
        raise TypeError("string contains no expressions")
    elif isinstance(body[-1], ast.Expr):
        body[-1] = ast.Return(body[-1].value)
        body[-1].lineno = body[-1].value.lineno
        body[-1].col_offset = body[-1].value.col_offset

    module = ast.parse("def fcn({0}): pass".format(", ".join(sorted(insymbols))))
    module.body[0].body = body
    return makefcn(compile(module, string, "exec"), math.__dict__, "fcn")

class ChainStep(object):
    def __init__(self, previous):
        self.previous = previous

    @property
    def source(self):
        return self.previous.source

    def _tofcn(self, expr):
        if isinstance(expr, parsable):
            expr = string2fcn(expr)
        return self.source._compilefcn(expr), expr.__code__.co_varnames

    def _tofcns(self, exprs):
        if isinstance(exprs, parsable):
            return [self._tofcn(exprs) + (exprs, exprs)]

        elif callable(exprs) and hasattr(exprs, "__code__"):
            return [self._tofcn(exprs) + (id(exprs), getattr(exprs, "__name__", None))]

        elif isinstance(exprs, dict) and all(isinstance(x, parsable) or (callable(x) and hasattr(x, "__code__")) for x in exprs.values()):
            return [self._tofcn(x) + (x if isinstance(x, parsable) else id(x), n) for n, x in exprs.items()]

        else:
            try:
                assert all(isinstance(x, parsable) or (callable(x) and hasattr(x, "__code__")) for x in exprs)
            except (TypeError, AssertionError):
                raise TypeError("exprs must be a dict of strings or functions, iterable of strings or functions, a single string, or a single function")
            else:
                return [self._tofcn(x) + ((x, x) if isinstance(x, parsable) else (id(x), getattr(x, "__name__", None))) for i, x in enumerate(exprs)]

    def _satisfy(self, requirement, branchnames, entryvars, entryvar, aliases):
        self.previous._satisfy(requirement, branchnames, entryvars, entryvar, aliases)

    def _argfcn(self, requirement, branchnames, entryvar, aliases, compilefcn):
        return self.previous._argfcn(requirement, branchnames, entryvar, aliases, compilefcn)

    def _compilefcns(self, fcns, entryvar, aliases, compilefcn):
        branchnames = []
        entryvars = set()
        for fcn, requirements, cacheid, dictname in fcns:
            for requirement in requirements:
                self._satisfy(requirement, branchnames, entryvars, entryvar, aliases)

        compiled = []
        for fcn, requirements, cacheid, dictname in fcns:
            argstrs = []
            argfcns = []
            env = {"fcn": fcn}
            for i, requirement in enumerate(requirements):
                argstrs.append("arg{0}(arrays)".format(i))
                argfcn = self._argfcn(requirement, branchnames, entryvar, aliases, compilefcn)
                argfcns.append(argfcn)
                env["arg{0}".format(i)] = argfcn

            module = ast.parse("def cfcn(arrays): return fcn({0})".format(", ".join(argstrs)))
            compiled.append(self.source._compilefcn(makefcn(compile(module, str(dictname), "exec"), env, "cfcn")))

        return compiled, branchnames, entryvars

    def _isidentifier(self, dictname):
        try:
            x = ast.parse(dictname)
            assert len(x.body) == 1 and isinstance(x.body[0], ast.Expr) and isinstance(x.body[0].value, ast.Name)
        except (SyntaxError, TypeError, AssertionError):
            return False
        else:
            return True

    def _compilefcn(self, numba):
        if callable(numba):
            return numba

        elif numba is None or numba is False:
            return lambda f: f

        elif numba is True:
            import numba as nb
            return lambda f: nb.njit()(f)

        else:
            import numba as nb
            return lambda f: nb.njit(**numba)(f)

    def iterate_apply(self, exprs, entrystepsize=100000, entrystart=None, entrystop=None, aliases={}, interpretations={}, entryvar=None, outputtype=dict, cache=None, basketcache=None, keycache=None, readexecutor=None, calcexecutor=None, numba=ifinstalled):
        compilefcn = self._compilefcn(numba)

        fcns = self._tofcns(exprs)

        dictnames = [dictname for fcn, requirements, cacheid, dictname in fcns]
        if outputtype == namedtuple:
            for dictname in dictnames:
                if not self._isidentifier(dictname):
                    raise ValueError("illegal field name for namedtuple: {0}".format(repr(dictname)))
            outputtype = namedtuple("Arrays", dictnames)

        if issubclass(outputtype, dict):
            def finish(results):
                return outputtype(zip(dictnames, results))
        elif outputtype == tuple or outputtype == list:
            def finish(results):
                return outputtype(results)
        else:
            def finish(results):
                return outputtype(*results)

        compiled, branchnames, entryvars = self._compilefcns(fcns, entryvar, aliases, compilefcn)

        excinfos = None
        for arrays in self.source._iterate(branchnames, len(entryvars) > 0, interpretations, entrystepsize, entrystart, entrystop, cache, basketcache, keycache, readexecutor):
            if excinfos is not None:
                for excinfo in excinfos:
                    _delayedraise(excinfo)
                yield finish(results)

            results = [None] * len(compiled)
            arrays = tuple(arrays)

            def calculate(i):
                try:
                    out = compiled[i](arrays)
                except:
                    return sys.exc_info()
                else:
                    results[i] = out
                    return None

            if calcexecutor is None:
                for i in range(len(compiled)):
                    uproot.tree._delayedraise(calculate(i))
                excinfos = ()
            else:
                excinfos = calcexecutor.map(calculate, range(len(compiled)))

        if excinfos is not None:
            for excinfo in excinfos:
                _delayedraise(excinfo)
            yield finish(results)

    def define(self, exprs={}, **more_exprs):
        return Define(self, exprs, **more_exprs)

class Define(ChainStep):
    def __init__(self, previous, exprs={}, **more_exprs):
        self.previous = previous

        if not isinstance(exprs, dict):
            raise TypeError("exprs must be a dict")
        exprs = dict(exprs)
        exprs.update(more_exprs)

        if any(not isinstance(x, parsable) or not self._isidentifier(x) or keyword.iskeyword(x) for x in exprs):
            raise TypeError("all names in exprs must be identifiers (and strings!)")

        self.fcn = {}
        self.requirements = {}
        for fcn, requirements, cacheid, dictname in self._tofcns(exprs):
            self.fcn[dictname] = fcn
            self.requirements[dictname] = requirements

    def _satisfy(self, requirement, branchnames, entryvars, entryvar, aliases):
        if requirement in self.requirements:
            for req in self.requirements[requirement]:
                self.previous._satisfy(req, branchnames, entryvars, entryvar, aliases)
        else:
            self.previous._satisfy(requirement, branchnames, entryvars, entryvar, aliases)

    def _argfcn(self, requirement, branchnames, entryvar, aliases, compilefcn):
        if requirement in self.requirements:
            argstrs = []
            argfcns = []
            env = {"fcn": self.fcn[requirement]}
            for i, req in enumerate(self.requirements[requirement]):
                argstrs.append("arg{0}(arrays)".format(i))
                argfcn = self.previous._argfcn(req, branchnames, entryvar, aliases, compilefcn)
                argfcns.append(argfcn)
                env["arg{0}".format(i)] = argfcn

            module = ast.parse("def cfcn(arrays): return fcn({0})".format(", ".join(argstrs)))
            return self.source._compilefcn(makefcn(compile(module, str(requirement), "exec"), env, "cfcn"))

        else:
            return self.previous._argfcn(requirement, branchnames, entryvar, aliases, compilefcn)

class ChainSource(ChainStep):
    def __init__(self, tree):
        self.tree = tree

    def _iterate(self, branchnames, hasentryvar, interpretations, entrystepsize, entrystart, entrystop, cache, basketcache, keycache, readexecutor):
        branches = {}
        for branchname in branchnames:
            if branchname in interpretations:
                branches[branchname] = interpretations[branchname]
            else:
                branches[branchname] = uproot.interp.auto.interpret(self.tree[branchname])

        for entrystart, entrystop, arrays in self.tree.iterate(entrystepsize = entrystepsize,
                                                               branches = branches,
                                                               outputtype = list,
                                                               reportentries = True,
                                                               entrystart = entrystart,
                                                               entrystop = entrystop,
                                                               cache = cache,
                                                               basketcache = basketcache,
                                                               keycache = keycache,
                                                               executor = readexecutor,
                                                               blocking = True):
            if hasentryvar:
                arrays.append(numpy.arange(entrystart, entrystop))
            yield arrays

    @property
    def source(self):
        return self

    def _satisfy(self, requirement, branchnames, entryvars, entryvar, aliases):
        if requirement == entryvar:
            entryvars.add(None)

        else:
            branchname = aliases.get(requirement, requirement)
            try:
                index = branchnames.index(branchname)
            except ValueError:
                index = len(branchnames)
                branchnames.append(branchname)

    def _argfcn(self, requirement, branchnames, entryvar, aliases, compilefcn):
        if requirement == entryvar:
            index = len(branchnames)
        else:
            branchname = aliases.get(requirement, requirement)
            index = branchnames.index(branchname)
        return compilefcn(lambda arrays: arrays[index])

class TTreeFunctionalMethods(uproot.tree.TTreeMethods):
    # makes __doc__ attribute mutable before Python 3.3
    __metaclass__ = type.__new__(type, "type", (uproot.tree.TTreeMethods.__metaclass__,), {})

    def iterate_apply(self, exprs, entrystepsize=100000, entrystart=None, entrystop=None, aliases={}, interpretations={}, entryvar=None, outputtype=dict, cache=None, basketcache=None, keycache=None, readexecutor=None, calcexecutor=None, numba=ifinstalled):
        return ChainSource(self).iterate_apply(exprs, entrystepsize, entrystart, entrystop, aliases, interpretations, entryvar, outputtype, cache, basketcache, keycache, readexecutor, calcexecutor, numba)

    def define(self, exprs={}, **more_exprs):
        return ChainSource(self).define(exprs, **more_exprs)

uproot.rootio.methods["TTree"] = TTreeFunctionalMethods
