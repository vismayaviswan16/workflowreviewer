"""Parser for the supported source / specification condition language.

Supported statement patterns (case-insensitive keywords):
  rule statement : [Otherwise,] IF <cond> THEN <route>
  default        : OTHERWISE|ELSE|All remaining tickets [->|THEN] <route>
Conditions:
  <cond> := <cond> OR <cond> | <cond> AND <cond> | NOT <cond> | ( <cond> ) | <atom>        (AND binds tighter than OR)
  <atom> := <field> is missing | <field> is present
          | <field> (< | <= | > | >= | == | = | !=) <value>
          | <field> [is] (at least | at most | more than | less than | exactly | not) <value>
          | <field> is <value>                    (equality)      | <field> in {v1, v2, ...}
  <value>: number [unit] | true | false | word | "quoted string"
Units (s, sec, seconds, min, minutes, h, hours) are converted to the field's canonical unit at parse time.
A unitless number on a unit-bearing field takes the artifact's DECLARED unit; with none declared it is
reported as ambiguous (Unsupported) -- it is never guessed.
Anything else raises Unsupported and stays visible in coverage.
"""
import re
from fractions import Fraction

from .ir import And, Cmp, Const, IsMissing, Not, Or

UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
_TOK = re.compile(r"""\s*(<=|>=|!=|==|=|<|>|\(|\)|\{|\}|,|"[^"]*"|'[^']*'|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*)""")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_ROUTE = re.compile(r"^[a-z][a-z0-9_]*$")


class Unsupported(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def tokenize(s):
    pos, out = 0, []
    s = s.strip()
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m:
            raise Unsupported(f"unrecognised text near {s[pos:pos + 20]!r}")
        out.append(m.group(1))
        pos = m.end()
        while pos < len(s) and s[pos].isspace():
            pos += 1
    return out


def norm_route(s):
    r = re.sub(r"[\s\-]+", "_", s.strip().lower().rstrip("."))
    if not _ROUTE.match(r):
        raise Unsupported(f"unrecognised route name {s!r}")
    return r


def convert_unit(value, lit_unit, declared_units, fdef, where="value"):
    """Convert a numeric literal to the field's canonical unit."""
    if fdef.unit is None:
        if lit_unit:
            raise Unsupported(f"unit {lit_unit!r} on unit-less field '{fdef.name}'")
        return Fraction(str(value))
    u = lit_unit or declared_units.get(fdef.name)
    if u is None:
        raise Unsupported(f"ambiguous unit for '{fdef.name}': no unit given and none declared")
    u = u.lower()
    if u not in UNIT_SECONDS or fdef.unit.lower() not in UNIT_SECONDS:
        raise Unsupported(f"unknown unit {u!r} for '{fdef.name}'")
    return Fraction(str(value)) * UNIT_SECONDS[u] / UNIT_SECONDS[fdef.unit.lower()]


class _P:
    def __init__(self, text, fields, units):
        self.t, self.i, self.fields, self.units = tokenize(text), 0, fields, units

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self):
        if self.i >= len(self.t):
            raise Unsupported("unexpected end of condition")
        self.i += 1
        return self.t[self.i - 1]

    def kw(self, w):
        p = self.peek()
        if p is not None and p.lower() == w:
            self.i += 1
            return True
        return False

    def expect(self, w):
        if not self.kw(w):
            raise Unsupported(f"expected {w!r}, found {self.peek()!r}")

    def parse(self):
        e = self.or_()
        if self.peek() is not None:
            raise Unsupported(f"unexpected trailing text {self.peek()!r}")
        return e

    def or_(self):
        items = [self.and_()]
        while self.kw("or"):
            items.append(self.and_())
        return items[0] if len(items) == 1 else Or(tuple(items))

    def and_(self):
        items = [self.unary()]
        while self.kw("and"):
            items.append(self.unary())
        return items[0] if len(items) == 1 else And(tuple(items))

    def unary(self):
        if self.kw("not"):
            return Not(self.unary())
        if self.peek() == "(":
            self.next()
            e = self.or_()
            if self.next() != ")":
                raise Unsupported("expected ')'")
            return e
        return self.atom()

    def atom(self):
        name = self.next()
        fd = self.fields.get(name) or self.fields.get(name.lower())
        if fd is None:
            raise Unsupported(f"unknown or undeclared field/word {name!r}")
        p = (self.peek() or "").lower()
        if p == "is":
            self.next()
            if self.kw("missing"):
                return IsMissing(fd.name)
            if self.kw("present"):
                return Not(IsMissing(fd.name))
            return Cmp(fd.name, self.phrase_op(default="=="), self.value(fd))
        if p in ("<", "<=", ">", ">=", "==", "=", "!="):
            self.next()
            return Cmp(fd.name, "==" if p == "=" else p, self.value(fd))
        if p == "in":
            self.next()
            close = {"{": "}", "(": ")"}.get(self.next())
            if close is None:
                raise Unsupported("expected '{' after 'in'")
            vals = [self.value(fd)]
            while self.peek() == ",":
                self.next()
                vals.append(self.value(fd))
            if self.next() != close:
                raise Unsupported("expected closing bracket")
            cs = tuple(Cmp(fd.name, "==", v) for v in vals)
            return cs[0] if len(cs) == 1 else Or(cs)
        if p in ("at", "more", "less", "exactly"):
            return Cmp(fd.name, self.phrase_op(default=None), self.value(fd))
        raise Unsupported(f"unsupported comparison after {name!r}: {self.peek()!r}")

    def phrase_op(self, default):
        if self.kw("not"):
            return "!="
        if self.kw("at"):
            if self.kw("least"):
                return ">="
            self.expect("most")
            return "<="
        if self.kw("more"):
            self.expect("than")
            return ">"
        if self.kw("less"):
            self.expect("than")
            return "<"
        if self.kw("exactly"):
            return "=="
        if default is None:
            raise Unsupported("unsupported comparison phrase")
        return default

    def value(self, fd):
        t = self.next()
        if fd.type == "number":
            if not _NUM.fullmatch(t):
                raise Unsupported(f"expected a number for '{fd.name}', found {t!r}")
            unit = None
            p = self.peek()
            if p is not None and p.lower() in UNIT_SECONDS:
                unit = self.next().lower()
            return convert_unit(t, unit, self.units, fd)
        if fd.type == "bool":
            if t.lower() in ("true", "false"):
                return t.lower() == "true"
            raise Unsupported(f"expected true/false for '{fd.name}', found {t!r}")
        if t[0] in "\"'":
            return t[1:-1]
        if t.lower() in ("and", "or", "not", "is"):
            raise Unsupported(f"expected a value for '{fd.name}', found keyword {t!r}")
        return t


def parse_condition(text, fields, units):
    if text.strip().lower() == "else":
        return Const(True)
    return _P(text, fields, units).parse()


_RULE = re.compile(r"^\s*(?:otherwise\s*,?\s*)?if\s+(?P<c>.+?)\s+then\s+(?P<r>[^.]+?)\s*\.?\s*$", re.I | re.S)
_DEFAULT = re.compile(r"^\s*(?:otherwise|else|all remaining tickets)\b\s*[,:]?\s*(?:->|then)?\s*(?P<r>[^.]+?)\s*\.?\s*$", re.I)


def parse_statement(text, fields, units):
    """-> ('rule', cond, route) | ('default', None, route); raises Unsupported."""
    m = _RULE.match(text)
    if m:
        return "rule", parse_condition(m.group("c"), fields, units), norm_route(m.group("r"))
    m = _DEFAULT.match(text)
    if m:
        return "default", None, norm_route(m.group("r"))
    raise Unsupported("statement is not in a supported pattern (IF <cond> THEN <route> | OTHERWISE <route>)")
