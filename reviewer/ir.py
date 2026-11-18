"""Shared IR for every artifact type: condition AST + three-valued evaluation + routers.

Missing-value semantics (documented contract):
  * Cmp on a missing field is False for every operator (including !=); `IsMissing` is the only test for absence.
  * Lifted Python keeps Python semantics: ordering comparison on None raises (ERR), `!=` on None is True.
Three-valued evaluation on a PARTIAL assignment returns True / False / None (unknown) / ERR.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional

ERR = "ERR"
NO_ROUTE = "<no_route>"
CYCLE = "<cycle>"
ERROR_ROUTE = "<error>"
OTHER = "__other__"


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str                      # number | bool | categorical
    unit: Optional[str] = None     # canonical unit for numbers (e.g. "min")
    integer: bool = False
    lo: Optional[Fraction] = None
    hi: Optional[Fraction] = None


@dataclass(frozen=True)
class Cmp:
    field: str
    op: str
    value: Any
    strict: bool = False           # lifted Python: ordering comparison raises on None


@dataclass(frozen=True)
class IsMissing:
    field: str


@dataclass(frozen=True)
class And:
    items: tuple


@dataclass(frozen=True)
class Or:
    items: tuple


@dataclass(frozen=True)
class Not:
    item: Any


@dataclass(frozen=True)
class Const:
    value: bool


@dataclass(frozen=True)
class Rule:
    id: str
    cond: Any
    route: str
    loc: str
    text: str = ""


@dataclass(frozen=True)
class DecisionList:
    rules: tuple
    default: Optional[str]


@dataclass(frozen=True)
class Outcome:
    route: str
    trace: tuple = ()


def _isnum(v):
    return isinstance(v, (int, float, Fraction)) and not isinstance(v, bool)


def num(v):
    return v if isinstance(v, Fraction) else Fraction(str(v))


_OPF = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}


def _cmp(v, op, c):
    if _isnum(v) and _isnum(c):
        # ints / Fractions compare exactly without conversion (hot path); floats go through Fraction(str(.))
        a = v if isinstance(v, (int, Fraction)) else Fraction(str(v))
        b = c if isinstance(c, (int, Fraction)) else Fraction(str(c))
        return _OPF[op](a, b)
    same = type(v) == type(c) and v == c
    if op == "==":
        return same
    if op == "!=":
        return not same
    return False


def eval3(n, a):
    """True/False/None(unknown)/ERR for node n under (possibly partial) assignment a."""
    t = type(n)
    if t is Cmp:
        if n.field not in a:
            return None
        v = a[n.field]
        if v is None:
            return ERR if n.strict else False
        return _cmp(v, n.op, n.value)
    if t is IsMissing:
        if n.field not in a:
            return None
        return a[n.field] is None
    if t is Const:
        return n.value
    if t is Not:
        v = eval3(n.item, a)
        if v is None or v == ERR:
            return v
        return not v
    if t is And:
        unk = False
        for it in n.items:
            v = eval3(it, a)
            if v is False:
                return False
            if v == ERR:
                return None if unk else ERR
            if v is None:
                unk = True
        return None if unk else True
    if t is Or:
        unk = False
        for it in n.items:
            v = eval3(it, a)
            if v is True:
                return True
            if v == ERR:
                return None if unk else ERR
            if v is None:
                unk = True
        return None if unk else False
    raise TypeError(f"bad node {n!r}")


def walk(n):
    yield n
    if isinstance(n, (And, Or)):
        for i in n.items:
            yield from walk(i)
    elif isinstance(n, Not):
        yield from walk(n.item)


def fields_of(n):
    return {x.field for x in walk(n) if isinstance(x, (Cmp, IsMissing))}


def constants_of(n):
    return [(x.field, x.value) for x in walk(n) if isinstance(x, Cmp)]


def _fmt(v):
    if isinstance(v, Fraction):
        return str(v.numerator) if v.denominator == 1 else f"{float(v):g}"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def show(n):
    t = type(n)
    if t is Cmp:
        return f"{n.field} {n.op} {_fmt(n.value)}"
    if t is IsMissing:
        return f"{n.field} is missing"
    if t is Const:
        return "true" if n.value else "false"
    if t is Not:
        return f"NOT ({show(n.item)})"
    sep = " AND " if t is And else " OR "
    return "(" + sep.join(show(i) for i in n.items) + ")"


def canon(n):
    """Syntactic canonical form (flatten + sort AND/OR). Used for approval-payload equality only;
    behavioural equivalence is decided on the partition grid, never on this string."""
    t = type(n)
    if t in (And, Or):
        parts = []
        for i in n.items:
            c = canon(i)
            parts.extend(c[1] if isinstance(c, tuple) and c[0] == t.__name__ else [c])
        return (t.__name__, sorted(parts, key=repr))
    if t is Not:
        return ("Not", canon(n.item))
    return show(n)


# ---------------------------------------------------------------- routers
class DLRouter:
    kind = "decision_list"

    def __init__(self, name, dl):
        self.name, self.dl = name, dl

    def fields(self):
        s = set()
        for r in self.dl.rules:
            s |= fields_of(r.cond)
        return s

    def route3(self, a):
        for r in self.dl.rules:
            v = eval3(r.cond, a)
            if v is True:
                return Outcome(r.route, (r.id,))
            if v is False:
                continue
            if v == ERR:
                return Outcome(ERROR_ROUTE, (r.id,))
            return None
        return Outcome(self.dl.default if self.dl.default is not None else NO_ROUTE, ("default",))


class CondRouter:
    """Wraps one condition so isolated-rule equivalence reuses the same exploration engine."""
    kind = "condition"

    def __init__(self, name, cond):
        self.name, self.cond = name, cond

    def fields(self):
        return fields_of(self.cond)

    def route3(self, a):
        v = eval3(self.cond, a)
        if v is None:
            return None
        return Outcome("ERR" if v == ERR else ("T" if v else "F"), ())


class WFRouter:
    kind = "workflow"

    def __init__(self, name, entry, nodes):
        self.name, self.entry, self.nodes = name, entry, nodes   # nodes: id -> dict(kind, route, branches[(cond,to)])

    def fields(self):
        s = set()
        for n in self.nodes.values():
            for c, _ in n.get("branches", []):
                s |= fields_of(c)
        return s

    def route3(self, a):
        node, seen, trace = self.entry, set(), []
        while True:
            if node not in self.nodes:
                return Outcome(f"<dangling:{node}>", tuple(trace))
            if node in seen:
                return Outcome(CYCLE, tuple(trace))
            seen.add(node)
            n = self.nodes[node]
            if n["kind"] == "terminal":
                return Outcome(n["route"], tuple(trace) + (node,))
            nxt = None
            for i, (c, to) in enumerate(n["branches"]):
                v = eval3(c, a)
                if v is True:
                    trace.append(f"{node}[{i}]")
                    nxt = to
                    break
                if v is False:
                    continue
                return None
            if nxt is None:
                return Outcome(NO_ROUTE, tuple(trace) + (f"{node}[none]",))
            node = nxt
