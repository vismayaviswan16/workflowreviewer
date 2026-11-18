"""Restricted Python subset -> IR. Inspection only: snippets are parsed with `ast`, NEVER executed.

Accepted module: optional docstring, optional `UNITS = {"field": "unit"}` literal, and one
`def route(<param>):` whose body is a sequence of
    if <cond>: return "<route>"            (also elif / else chains, each branch a single return of a string)
    return "<route>"                        (the default; anything after it is reported as unreachable)
    return "<a>" if <cond> else "<b>"       (conditional expressions, nestable; expanded into ordered rules)
Straight-line aliases before use: `w = <param>.get("field")` (also with a literal default), never reassigned.
Conditions: and / or / not, `x is None`, `x is not None`, `x is True/False` (bool fields), ==, !=, <, <=, >, >=,
`x in (<lits>)` / `x not in (<lits>)` (non-empty literal tuple/list/set), and bare truthiness of a bool field, where x is `<param>.get("field")`, `<param>.get("field", <literal>)` or such an alias, and the other side is a literal.
CONTRACT: for `.get("f", default)` a missing ticket value means the key is ABSENT (default applies); an explicit
null value is outside the contract. `.get("f", default)` is only supported in comparisons, not in `is None` tests.
Python semantics are preserved: `None < 5` raises (reported as a crash on missing values unless guarded),
`None != c` is True. Everything else is recorded as unsupported (visible in coverage).
Three stages are kept separate in the report: syntax check, inspection (this lifter), execution (not done).
"""
import ast
import io
import tokenize
from dataclasses import dataclass, field
from fractions import Fraction

from .ir import And, Cmp, DecisionList, IsMissing, Not, Or, Rule, _cmp
from .lang import Unsupported, convert_unit

_OPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


@dataclass
class LiftResult:
    syntax_ok: bool
    syntax_error: str = None
    dl: DecisionList = None
    unsupported: list = field(default_factory=list)   # (location, reason)
    comments: list = field(default_factory=list)      # (line, text)
    units: dict = field(default_factory=dict)

    @property
    def complete(self):
        return self.syntax_ok and self.dl is not None and not self.unsupported


def _comments(src):
    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type == tokenize.COMMENT:
                out.append((t.start[0], t.string.lstrip("# ").strip()))
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _guards_and(c):
    if isinstance(c, Not) and isinstance(c.item, IsMissing):
        return {c.item.field}
    if isinstance(c, And):
        return set().union(*[_guards_and(i) for i in c.items])
    return set()


def _guards_or(c):
    if isinstance(c, IsMissing):
        return {c.field}
    if isinstance(c, Or):
        return set().union(*[_guards_or(i) for i in c.items])
    return set()


class _Lifter:
    def __init__(self, src, fields, units, param):
        self.src, self.fields, self.units, self.param = src, fields, units, param
        self.alias = {}

    NOSET = object()

    def access(self, n):
        """-> (field, default_node | NOSET)"""
        if isinstance(n, ast.Subscript):
            raise Unsupported('subscript access can raise KeyError; use .get("field")')
        if isinstance(n, ast.Name) and n.id in self.alias:
            return self.alias[n.id]
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == self.param
                and 1 <= len(n.args) <= 2 and not n.keywords
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            f = n.args[0].value
            if f not in self.fields:
                raise Unsupported(f"undeclared field {f!r}")
            if len(n.args) == 2 and not (isinstance(n.args[1], ast.Constant) and n.args[1].value is None):
                if not isinstance(n.args[1], ast.Constant):
                    raise Unsupported("non-literal default in .get()")
                return f, n.args[1]
            return f, self.NOSET
        raise Unsupported(f"unsupported expression {ast.unparse(n)!r}")

    def literal(self, f, node):
        if not isinstance(node, ast.Constant):
            raise Unsupported(f"non-literal comparison value {ast.unparse(node)!r}")
        v, fd = node.value, self.fields[f]
        if fd.type == "bool":
            if isinstance(v, bool):
                return v
            raise Unsupported(f"bool field {f!r} compared with non-bool")
        if fd.type == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise Unsupported(f"number field {f!r} compared with non-number")
            return convert_unit(v, None, self.units, fd)
        if isinstance(v, str):
            return v
        raise Unsupported(f"categorical field {f!r} compared with non-string")

    def cond(self, n, safe):
        if isinstance(n, ast.BoolOp):
            is_and = isinstance(n.op, ast.And)
            local, items = set(safe), []
            for v in n.values:
                c = self.cond(v, local)
                items.append(c)
                local |= _guards_and(c) if is_and else _guards_or(c)
            return (And if is_and else Or)(tuple(items))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return Not(self.cond(n.operand, safe))
        if isinstance(n, ast.Compare):
            if len(n.ops) != 1:
                raise Unsupported("chained comparison")
            op, right = n.ops[0], n.comparators[0]
            f, dflt = self.access(n.left)
            has_d = dflt is not self.NOSET
            if isinstance(op, (ast.In, ast.NotIn)):
                if has_d:
                    raise Unsupported("`in` on .get() with a default is not supported")
                if not isinstance(right, (ast.Tuple, ast.List, ast.Set)) or not right.elts:
                    raise Unsupported("`in` is only supported against a non-empty literal tuple/list/set")
                if self.fields[f].type == "bool":
                    raise Unsupported(f"`in` on bool field {f!r} is not supported")
                items = tuple(Cmp(f, "==", self.literal(f, e)) for e in right.elts)
                c = items[0] if len(items) == 1 else Or(items)     # Python: None in (...) is False, None not in (...) is True
                return c if isinstance(op, ast.In) else Not(c)
            if isinstance(op, (ast.Is, ast.IsNot)):
                if has_d:
                    raise Unsupported("`is None` on .get() with a default is not supported")
                if isinstance(right, ast.Constant) and right.value is None:
                    return IsMissing(f) if isinstance(op, ast.Is) else Not(IsMissing(f))
                if isinstance(right, ast.Constant) and isinstance(right.value, bool) and self.fields[f].type == "bool":
                    c = Cmp(f, "==", right.value)                  # `x is True` on a bool field; missing -> False
                    return c if isinstance(op, ast.Is) else Not(c)
                raise Unsupported("`is` is only supported against None or True/False on bool fields")
            if type(op) in _OPS:
                if self.fields[f].type != "number":
                    raise Unsupported(f"ordering comparison on non-number field {f!r}")
                lit = self.literal(f, right)
                if has_d:       # missing -> default, so no crash; missing satisfies the test iff default op lit holds
                    c = Cmp(f, _OPS[type(op)], lit)
                    return Or((IsMissing(f), c)) if _cmp(self.literal(f, dflt), _OPS[type(op)], lit) else c
                return Cmp(f, _OPS[type(op)], lit, strict=f not in safe)
            if isinstance(op, (ast.Eq, ast.NotEq)):
                lit = self.literal(f, right)
                eq = isinstance(op, ast.Eq)
                if has_d:
                    dv = self.literal(f, dflt)
                    miss_true = (dv == lit) if eq else (dv != lit)
                    c = Cmp(f, "==" if eq else "!=", lit)
                    return Or((IsMissing(f), c)) if miss_true else c
                if eq:
                    return Cmp(f, "==", lit)
                return Or((IsMissing(f), Cmp(f, "!=", lit)))      # Python: None != c is True
            raise Unsupported(f"unsupported operator {type(op).__name__}")
        f, dflt = self.access(n)                     # bare truthiness
        if dflt is not self.NOSET:
            raise Unsupported("truthiness of .get() with a default is not supported")
        if self.fields[f].type != "bool":
            raise Unsupported(f"truthiness test only supported for bool fields ({f!r})")
        return Cmp(f, "==", True)

    def single_return(self, body, uns):
        if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            return body[0].value.value
        uns.append((f"line {body[0].lineno}", "branch body must be exactly `return \"<route>\"`"))
        return None

    def expr(self, n, safe):
        """Return expression -> ordered [(cond | None, route)]; a conditional expression expands into complementary rules."""
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return [(None, n.value)]
        if isinstance(n, ast.IfExp):
            c = self.cond(n.test, safe)
            sg = safe | _guards_and(c)
            out = []
            for x, r in self.expr(n.body, sg):
                out.append((c if x is None else And((c, x)), r))
            for x, r in self.expr(n.orelse, safe):
                nc = Not(c)
                out.append((nc if x is None else And((nc, x)), r))
            return out
        raise Unsupported("return value must be a string literal or a conditional expression of string literals")

    def returns(self, body, uns, safe):
        """Body of a branch: exactly one `return <expr>` -> ordered [(cond | None, route)] or None (reported)."""
        if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
            try:
                return self.expr(body[0].value, safe)
            except Unsupported as u:
                uns.append((f"line {body[0].lineno}", u.reason))
                return None
        uns.append((f"line {body[0].lineno}", "branch body must be exactly one `return <route or conditional>`"))
        return None


def lift_python(src, fields):
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return LiftResult(False, f"line {e.lineno}: {e.msg}")
    res = LiftResult(True, comments=_comments(src))
    func = None
    for st in tree.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            continue
        if (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                and st.targets[0].id == "UNITS"):
            try:
                u = ast.literal_eval(st.value)
                assert isinstance(u, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in u.items())
                res.units = u
            except Exception:
                res.unsupported.append((f"line {st.lineno}", "UNITS must be a dict literal of str -> str"))
            continue
        if isinstance(st, ast.FunctionDef) and st.name == "route" and func is None:
            func = st
            continue
        res.unsupported.append((f"line {st.lineno}", f"unsupported top-level {type(st).__name__}"))
    if func is None:
        res.unsupported.append(("module", "no function named `route`"))
        return res
    a = func.args
    if len(a.args) != 1 or a.vararg or a.kwarg or a.kwonlyargs or a.defaults:
        res.unsupported.append((f"line {func.lineno}", "route must take exactly one plain parameter"))
        return res
    lf = _Lifter(src, fields, res.units, a.args[0].arg)
    rules, default, safe, done = [], None, set(), False
    for st in func.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            continue
        if done:
            res.unsupported.append((f"line {st.lineno}", "unreachable code after a default return"))
            continue
        if isinstance(st, ast.Assign):
            try:
                if len(st.targets) != 1 or not isinstance(st.targets[0], ast.Name):
                    raise Unsupported("only `name = <param>.get(...)` assignments are supported")
                nm = st.targets[0].id
                if nm in lf.alias or nm == a.args[0].arg:
                    raise Unsupported(f"reassignment of {nm!r} is not supported")
                lf.alias[nm] = lf.access(st.value)
            except Unsupported as u:
                res.unsupported.append((f"line {st.lineno}", u.reason))
            continue
        if isinstance(st, ast.Return):
            try:
                ex = lf.expr(st.value, safe) if st.value is not None else None
                if ex is None:
                    raise Unsupported("return value must be a string literal or a conditional expression of string literals")
                for k, (x, r) in enumerate(ex[:-1]):
                    rules.append(Rule(f"L{st.lineno}.{k}", x, r, f"line {st.lineno}", ast.get_source_segment(src, st.value) or ""))
                default, done = ex[-1][1], True       # last expansion is the complement of the earlier ones
            except Unsupported as u:
                res.unsupported.append((f"line {st.lineno}", u.reason))
        elif isinstance(st, ast.If):
            node = st
            while True:
                c = None
                try:
                    c = lf.cond(node.test, safe)
                except Unsupported as u:
                    res.unsupported.append((f"line {node.test.lineno}", u.reason))
                ex = lf.returns(node.body, res.unsupported, safe | (_guards_and(c) if c is not None else set()))
                if c is not None and ex is not None:
                    for k, (x, r) in enumerate(ex):
                        rid = f"L{node.lineno}" if len(ex) == 1 else f"L{node.lineno}.{k}"
                        rules.append(Rule(rid, c if x is None else And((c, x)), r, f"line {node.lineno}",
                                          ast.get_source_segment(src, node.test) or ""))
                if isinstance(c, IsMissing):
                    safe = safe | {c.field}          # earlier early-return handled the missing case
                if not node.orelse:
                    break
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    node = node.orelse[0]
                    continue
                ex = lf.returns(node.orelse, res.unsupported, safe)
                if ex is not None:
                    for k, (x, r) in enumerate(ex[:-1]):
                        rules.append(Rule(f"L{node.orelse[0].lineno}.{k}", x, r, f"line {node.orelse[0].lineno}", ast.unparse(node.orelse[0].value)))
                    default, done = ex[-1][1], True
                break
        else:
            res.unsupported.append((f"line {st.lineno}", f"unsupported statement {type(st).__name__}"))
    res.dl = DecisionList(tuple(rules), default)
    return res
