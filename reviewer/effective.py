"""Effective requirements = source requirements + applicable approved changes.

Contract (documented, deterministic):
  * Only TOP-LEVEL approval records with status == "approved" count. Prose claims never do.
  * Scope: a record applies iff applies_to == "*" or review_version is listed.
  * Ordering: records are replayed by (seq, id). A record that lists another in `supersedes` retires it.
  * Among the remaining ("live") records for one requirement, each attribute (condition, route) may be
    set by at most one distinct value. Two live records that set the same attribute to different values
    -- including equal seq, or later seq WITHOUT `supersedes` -- are a CONFLICT: the reviewer does not
    pick one; the requirement is reported as unresolved and the review cannot PASS.
  * A `supersedes` link is honoured only if it names an existing record of the SAME target with an earlier-or-equal seq.
    Unknown ids, cross-target links, links to a later seq and supersession cycles are ambiguities (target unresolved).
  * REVOCATION is an event, not a state: a record with status "revoked" (in scope) retires the approvals listed in its
    `supersedes` (same target, earlier-or-equal seq) and carries no payload; the requirement reverts to the next live
    record or to its source text. A revocation that names nothing / unknown / cross-target / later records is an ambiguity.
    A revoked record that is merely listed with status "revoked" and no one revoking it is ignored (not approved).
  * Records for unknown / unsupported targets, or whose payload is outside the supported language,
    are reported as ambiguities.
"""
import re
from dataclasses import dataclass, field

from .ir import DecisionList, Rule, canon, show
from .lang import Unsupported, norm_route, parse_condition, parse_statement

_INJECT = re.compile(r"\b(ignore|disregard|override|system\s*:|instruction|report (?:status )?pass|mark (?:this )?pass|"
                     r"return pass|you must|do not report)\b", re.I)
_APPROVAL_CLAIM = re.compile(r"\b(approved|approval|signed off|authori[sz]ed)\b", re.I)


def scan_untrusted(text):
    flags = []
    if _INJECT.search(text or ""):
        flags.append("instruction-like text")
    if _APPROVAL_CLAIM.search(text or ""):
        flags.append("approval claim in prose")
    return flags


@dataclass
class EffRule:
    id: str
    order: float
    cond: object
    route: str
    anchor: str
    text: str
    base_text: str
    applied: list = field(default_factory=list)


@dataclass
class Effective:
    rules: list = field(default_factory=list)
    default: str = None
    default_id: str = None
    default_anchor: str = ""
    unsupported: list = field(default_factory=list)   # {id, anchor, text, reason}
    conflicts: list = field(default_factory=list)
    ambiguities: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    applied: list = field(default_factory=list)
    untrusted: list = field(default_factory=list)
    extra_conds: list = field(default_factory=list)    # base / rejected-payload conds, only for grid constants
    unresolved_ids: set = field(default_factory=set)

    @property
    def complete(self):
        return not (self.unsupported or self.conflicts or self.ambiguities)

    @property
    def first_unresolved_order(self):
        return getattr(self, "_first_unres", None)

    def dl(self):
        return DecisionList(tuple(Rule(r.id, r.cond, r.route, r.anchor, r.text) for r in self.rules), self.default)


def derive(bundle, apply_approvals=True):
    E = Effective()
    fields, units = bundle.fields, bundle.requirements.units
    parsed = {}
    items = sorted(bundle.requirements.items, key=lambda x: (x.order, x.id))
    for it in items:
        if it.note:
            fl = scan_untrusted(it.note)
            if fl:
                E.untrusted.append({"artifact": "requirements", "location": f"{it.anchor} (note of {it.id})",
                                    "text": it.note, "flags": fl, "effect": "ignored"})
        try:
            kind, cond, route = parse_statement(it.text, fields, units)
            parsed[it.id] = dict(kind=kind, cond=cond, route=route, item=it)
            if cond is not None:
                E.extra_conds.append(cond)
        except Unsupported as u:
            parsed[it.id] = None
            E.unsupported.append({"id": it.id, "anchor": it.anchor, "text": it.text, "reason": u.reason})
            E.unresolved_ids.add(it.id)
    applicable, revokers = [], []
    for a in sorted(bundle.approvals, key=lambda a: (a.seq, a.id)):
        if a.status == "revoked" and (a.applies_to == "*" or bundle.review_version in a.applies_to) and apply_approvals:
            revokers.append(a)        # a revocation is an EVENT: it retires the records it names (see contract)
            continue
        if a.status != "approved":
            E.ignored.append({"id": a.id, "reason": f"status={a.status}"})
        elif a.applies_to != "*" and bundle.review_version not in a.applies_to:
            E.ignored.append({"id": a.id, "reason": f"scope {a.applies_to} does not include {bundle.review_version}"})
        elif not apply_approvals:
            E.ignored.append({"id": a.id, "reason": "approvals disabled (ablation)"})
        else:
            applicable.append(a)
    # ---- supersession links. A link is honoured only if it is unambiguous; otherwise the affected targets are
    # reported as unresolved (never silently resolved to the base text or to a convenient record).
    all_by_id = {a.id: a for a in bundle.approvals}
    links, bad_targets = {}, set()

    def _amb(ids, target, reason):
        E.ambiguities.append({"approvals": sorted(ids), "target": target, "reason": reason})
        E.unresolved_ids.add(target)
        bad_targets.add(target)

    # ---- revocation events: a `revoked` record names (in `supersedes`) the approval(s) it withdraws. It carries no payload.
    # Valid only if each named record exists, has the SAME target and an earlier-or-equal seq; otherwise ambiguity (INCOMPLETE).
    retracted = {}
    for rv in revokers:
        if not rv.supersedes:
            _amb([rv.id], rv.target, f"revocation {rv.id} names no approval to revoke (list it in `supersedes`)")
            continue
        for sid in rv.supersedes:
            o = all_by_id.get(sid)
            if o is None:
                _amb([rv.id], rv.target, f"revocation {rv.id} names unknown approval id {sid!r}")
            elif o.target != rv.target:
                _amb([rv.id, sid], rv.target, f"revocation {rv.id} (target {rv.target}) names {sid} (target {o.target}): cross-target revocation is not allowed")
                E.unresolved_ids.add(o.target); bad_targets.add(o.target)
            elif o.seq > rv.seq:
                _amb([rv.id, sid], rv.target, f"revocation {rv.id} (seq {rv.seq}) precedes the record it revokes {sid} (seq {o.seq})")
            else:
                retracted[sid] = rv.id
    for a in list(applicable):
        if a.id in retracted:
            applicable.remove(a)
            E.ignored.append({"id": a.id, "reason": f"revoked by {retracted[a.id]}"})
    live_ids = {a.id for a in applicable}

    for a in applicable:
        for sid in a.supersedes:
            o = all_by_id.get(sid)
            if o is None:
                _amb([a.id], a.target, f"{a.id} supersedes unknown approval id {sid!r}")
            elif o.target != a.target:
                _amb([a.id, sid], a.target, f"{a.id} (target {a.target}) supersedes {sid} (target {o.target}): cross-target supersession is not allowed")
                E.unresolved_ids.add(o.target)
                bad_targets.add(o.target)
            elif sid in live_ids and o.seq > a.seq:
                _amb([a.id, sid], a.target, f"{a.id} (seq {a.seq}) supersedes {sid} with a LATER seq {o.seq}")
            elif sid in live_ids:
                links.setdefault(a.id, set()).add(sid)

    def _reaches(src, dst, seen=None):
        seen = seen if seen is not None else set()
        for n in links.get(src, ()):
            if n == dst:
                return True
            if n not in seen:
                seen.add(n)
                if _reaches(n, dst, seen):
                    return True
        return False

    cyc = {n for n in links if _reaches(n, n)}
    for n in sorted(cyc):
        _amb(sorted({n} | links[n]), all_by_id[n].target, f"supersession cycle involving {n}: contradictory records, no version chosen")
    superseded = {t for src, ts in links.items() if src not in cyc for t in ts}
    by_target = {}
    for a in applicable:
        if a.id in superseded:
            E.ignored.append({"id": a.id, "reason": "superseded"})
        else:
            by_target.setdefault(a.target, []).append(a)
    changes = {}
    for target, recs in by_target.items():
        if target in bad_targets:
            E.unresolved_ids.add(target)
            continue
        if parsed.get(target) is None:
            why = "target requirement not found" if target not in parsed else "target requirement is unsupported"
            E.ambiguities.append({"approvals": [a.id for a in recs], "target": target, "reason": why})
            E.unresolved_ids.add(target)
            continue
        slots = {"cond": {}, "route": {}}
        bad = False
        for a in recs:
            u = {**units, **a.units}
            try:
                if a.new_when is not None:
                    c = parse_condition(a.new_when, fields, u)
                    E.extra_conds.append(c)
                    slots["cond"].setdefault(repr(canon(c)), (c, []))[1].append(a.id)
                if a.new_then is not None:
                    r = norm_route(a.new_then)
                    slots["route"].setdefault(r, (r, []))[1].append(a.id)
            except Unsupported as ex:
                E.ambiguities.append({"approvals": [a.id], "target": target, "reason": f"payload unsupported: {ex.reason}"})
                bad = True
        for attr, d in slots.items():
            if len(d) > 1:
                E.conflicts.append({"target": target, "attribute": attr, "approvals": sorted({i for _, ids in d.values() for i in ids}),
                                    "values": [show(v) if attr == "cond" else v for v, _ in d.values()],
                                    "reason": "two live approved records set different values with no `supersedes` relation"})
                bad = True
        if bad:
            E.unresolved_ids.add(target)
            continue
        changes[target] = {k: (list(d.values())[0] if d else None) for k, d in slots.items()}
    unres_orders = []
    for it in items:
        p = parsed[it.id]
        if p is None:
            unres_orders.append(it.order)
            continue
        cond, route, applied = p["cond"], p["route"], []
        if it.id in changes:
            ch = changes[it.id]
            if ch["cond"]:
                cond = ch["cond"][0]; applied += ch["cond"][1]
            if ch["route"]:
                route = ch["route"][0]; applied += ch["route"][1]
            applied = sorted(set(applied))
            E.applied.append({"requirement": it.id, "approvals": applied})
        if it.id in E.unresolved_ids:
            unres_orders.append(it.order)
            continue
        if p["kind"] == "default":
            if E.default is not None:
                E.ambiguities.append({"approvals": [], "target": it.id, "reason": "more than one default requirement"})
            E.default, E.default_id, E.default_anchor = route, it.id, it.anchor
        else:
            E.rules.append(EffRule(it.id, it.order, cond, route, it.anchor,
                                   f"{show(cond)} -> {route}", it.text, applied))
    if E.default is None and not any(p and p["kind"] == "default" for p in parsed.values()) and not E.unsupported:
        E.ambiguities.append({"approvals": [], "target": None, "reason": "no default (OTHERWISE) requirement"})
    E._first_unres = min(unres_orders) if unres_orders else None
    return E
