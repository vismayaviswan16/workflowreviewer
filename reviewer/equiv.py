"""Equivalence by partition grid.

Every condition in the supported language is a boolean combination of threshold / equality / missing tests,
so its truth value can only change at the constants that appear in some artifact. One representative per
cell of the per-field partition (each constant, a point in every gap, below/above, MISSING, plus an OTHER
value for categoricals) is therefore EXHAUSTIVE for the supported language -- not a sample.

Pruning is exact: fields are assigned one at a time; when every router already has a definite route
under the partial assignment (three-valued evaluation), the whole subtree is decided and is not expanded.
If the number of evaluated leaves exceeds the cap, we fall back to a labelled SAMPLED mode
(boundary probes + pairwise coverage + seeded random). Sampled mismatches are valid counterexamples;
sampled agreement is never evidence of equivalence.
"""
import itertools
import math
import random
import time
from dataclasses import dataclass, field
from fractions import Fraction

from .ir import OTHER, constants_of, num


class BudgetExceeded(Exception):
    pass


@dataclass
class Grid:
    order: list                      # field names, exploration order
    reps: dict                       # field -> list of representative values
    defs: dict

    @property
    def total(self):
        n = 1
        for f in self.order:
            n *= len(self.reps[f])
        return n

    def subset(self, names):
        names = [f for f in self.order if f in names]
        return Grid(names, {f: self.reps[f] for f in names}, self.defs)


def _number_reps(fd, consts):
    """Representatives for a numeric field: every constant, one point inside every open gap, all clipped
    to the declared domain [lo, hi]; integer fields only get integer points (empty gaps are skipped)."""
    cs = sorted({num(c) for c in consts})
    lo, hi = fd.lo, fd.hi
    pts = {c for c in cs if (lo is None or c >= lo) and (hi is None or c <= hi) and (not fd.integer or c.denominator == 1)}
    bounds = [None] + cs + [None]
    for a, b in zip(bounds, bounds[1:]):
        if fd.integer:
            lowest = math.floor(a) + 1 if a is not None else None
            highest = math.ceil(b) - 1 if b is not None else None
            if lo is not None:
                lowest = max(lowest, math.ceil(lo)) if lowest is not None else math.ceil(lo)
            if hi is not None:
                highest = min(highest, math.floor(hi)) if highest is not None else math.floor(hi)
            if lowest is None and highest is None:
                cand = 0
            elif lowest is None:
                cand = highest
            elif highest is None:
                cand = lowest
            elif lowest > highest:
                continue
            else:
                cand = highest if a is None else lowest      # prefer the value adjacent to a constant
        else:
            left, right, lc, rc = a, b, False, False
            if lo is not None and (left is None or lo > left):
                left, lc = lo, True
            if hi is not None and (right is None or hi < right):
                right, rc = hi, True
            if left is None and right is None:
                cand = Fraction(0)
            elif left is None:
                cand = right - 1
            elif right is None:
                cand = left + 1
            elif left < right:
                cand = (left + right) / 2
            elif left == right and lc and rc:
                cand = left
            else:
                continue
        pts.add(Fraction(cand))
    return sorted(pts)


def _reps(fd, consts):
    if fd.type == "bool":
        return [True, False, None]
    if fd.type == "categorical":
        return sorted({c for c in consts if isinstance(c, str)}) + [OTHER, None]
    out = _number_reps(fd, consts)
    return [int(p) if p.denominator == 1 else p for p in out] + [None]


def build_grid(fdefs, conds, referenced_by_count=None):
    """fdefs: name->FieldDef; conds: every condition AST of every artifact (incl. superseded values)."""
    consts = {}
    used = set()
    count = {}
    for c in conds:
        for f, v in constants_of(c):
            consts.setdefault(f, []).append(v)
        from .ir import fields_of
        for f in fields_of(c):
            used.add(f)
            count[f] = count.get(f, 0) + 1
    names = sorted(used, key=lambda f: (-count[f], f))
    reps = {f: _reps(fdefs[f], consts.get(f, [])) for f in names}
    return Grid(names, reps, fdefs)


@dataclass
class Region:
    assign: dict                     # partial assignment that fully decides every router
    covered: int                     # number of grid cells represented by this leaf
    outs: dict                       # router name -> Outcome


@dataclass
class Result:
    mode: str                        # exhaustive | sampled
    cells_total: int
    leaves: int
    cells_covered: int
    regions: list = field(default_factory=list)
    fired: dict = field(default_factory=dict)
    seed: int = None
    note: str = ""


def explore(routers, grid, max_leaves, prune=True, deadline=None):
    """Exact pruned exploration. routers: name -> router with .route3(assign)."""
    order = grid.order
    suffix = [1] * (len(order) + 1)
    for i in range(len(order) - 1, -1, -1):
        suffix[i] = suffix[i + 1] * len(grid.reps[order[i]])
    res = Result("exhaustive", grid.total, 0, 0)
    res.fired = {n: set() for n in routers}

    def leaf(assign, outs, i):
        res.leaves += 1
        if res.leaves > max_leaves:
            raise BudgetExceeded()
        res.cells_covered += suffix[i]
        for n, o in outs.items():
            res.fired[n].add(o.trace[0] if o.trace else "")
            if o.trace and len(o.trace) > 1:
                res.fired[n].update(o.trace)
        res.regions.append(Region(dict(assign), suffix[i], outs))

    ticks = [0]

    def rec(assign, i):
        ticks[0] += 1
        if deadline is not None and (ticks[0] & 255) == 0 and time.monotonic() > deadline:
            raise BudgetExceeded()
        outs = {n: r.route3(assign) for n, r in routers.items()} if (prune or i == len(order)) else None
        if outs is not None and all(o is not None for o in outs.values()):
            leaf(assign, outs, i)
            return
        if i == len(order):
            raise AssertionError("undecided router on a complete assignment")
        f = order[i]
        for v in grid.reps[f]:
            assign[f] = v
            rec(assign, i + 1)
        del assign[f]

    rec({}, 0)
    return res


def full_cross(routers, grid):
    """Unpruned reference enumeration (used to prove pruning exact in tests)."""
    out = {}
    for combo in itertools.product(*[grid.reps[f] for f in grid.order]):
        a = dict(zip(grid.order, combo))
        out[combo] = {n: r.route3(a) for n, r in routers.items()}
    return out


def sample_cells(grid, cap, seed):
    """Boundary probes -> pairwise coverage -> seeded random fill, truncated at cap. Deterministic."""
    rng = random.Random(seed)
    order, reps = grid.order, grid.reps
    seen, cells = set(), []

    def add(d):
        key = tuple(d[f] if not isinstance(d[f], Fraction) else ("F", d[f]) for f in order)
        if key not in seen and len(cells) < cap:
            seen.add(key)
            cells.append(dict(d))

    def rand_cell():
        return {f: rng.choice(reps[f]) for f in order}

    typical = {f: [reps[f][0], reps[f][len(reps[f]) // 2], None] for f in order}
    for f in order:                                     # 1) boundary probes
        for v in reps[f]:
            for t in range(3):
                d = {g: typical[g][t] for g in order}
                d[f] = v
                add(d)
    for f1, f2 in itertools.combinations(order, 2):     # 2) pairwise coverage
        for v1 in reps[f1]:
            for v2 in reps[f2]:
                d = rand_cell()
                d[f1], d[f2] = v1, v2
                add(d)
    tries = 0
    while len(cells) < cap and tries < cap * 20:        # 3) seeded random fill
        add(rand_cell())
        tries += 1
    return cells


def sampled(routers, grid, cap, seed, cells=None, mode="sampled", note=""):
    cells = cells if cells is not None else sample_cells(grid, cap, seed)
    res = Result(mode, grid.total, len(cells), len(cells), seed=seed, note=note)
    res.fired = {n: set() for n in routers}
    for a in cells:
        outs = {n: r.route3(a) for n, r in routers.items()}
        for n, o in outs.items():
            res.fired[n].add(o.trace[0] if o.trace else "")
            res.fired[n].update(o.trace)
        res.regions.append(Region(a, 1, outs))
    return res


def random_cells(grid, n, seed, lo_hi=None):
    """ABLATION ONLY: uniform random tickets, ignoring the partition (numbers uniform on [0, 2*max const])."""
    rng = random.Random(seed)
    cells = []
    for _ in range(n):
        d = {}
        for f in grid.order:
            fd = grid.defs[f]
            r = grid.reps[f]
            if fd.type == "number":
                top = max([abs(float(x)) for x in r if x is not None] + [1.0]) * 2
                v = rng.uniform(0, top)
                v = int(v) if fd.integer else round(v, 2)
                d[f] = None if rng.random() < 0.15 else v
            else:
                d[f] = rng.choice(r)
        cells.append(d)
    return cells
