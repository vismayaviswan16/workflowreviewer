"""Input contracts. A review bundle is one JSON object:

bundle_id:str  review_version:str   (the artifact version under review; approvals are scoped to it)
fields:{name:{type:number|bool|categorical, unit?:str, integer?:bool, min?:num, max?:num}}
requirements:{id, version, units?:{field:unit}, items:[{id, anchor, order:num, text, note?}]}
    text = ONE normative statement in the supported language; note = free prose (untrusted, never parsed for semantics)
approvals:[{id, target:<requirement id>, status:approved|proposed|rejected|revoked, seq:int,
            applies_to:"*"|[version,...], new_when?:cond, new_then?:route, supersedes?:[approval id], units?}]
    top-level approval metadata is TRUSTED; claims inside prose are not.
spec:{id, version, units?, rules:[{id, source_ref?, when, then}], default?}          (rule order = precedence)
workflow:{id, version, units?, entry, nodes:[{id, version?, kind:decision|terminal, branches?:[{when,to}], route?}]}
snippets:[{id, version, linked_node?, source}]                                       (restricted Python subset)
review_config?:{grid_cap, require_model, ...}   (operator configuration; whitelisted keys only)
Every artifact carries a version; every finding cites artifact + version + location.
"""
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Optional

from .ir import FieldDef

STATUSES_OK = {"approved", "proposed", "rejected", "revoked"}


class SchemaError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Req:
    id: str; anchor: str; order: float; text: str; note: str = ""


@dataclass
class Requirements:
    id: str; version: str; units: dict; items: list


@dataclass
class Approval:
    id: str; target: str; status: str; seq: int; applies_to: Any
    new_when: Optional[str] = None; new_then: Optional[str] = None
    supersedes: list = field(default_factory=list); units: dict = field(default_factory=dict)


@dataclass
class SpecRule:
    id: str; when: str; then: str; source_ref: Optional[str] = None


@dataclass
class Spec:
    id: str; version: str; units: dict; rules: list; default: Optional[str]


@dataclass
class Node:
    id: str; kind: str; version: Optional[str] = None; branches: list = field(default_factory=list); route: Optional[str] = None


@dataclass
class Workflow:
    id: str; version: str; units: dict; entry: str; nodes: list


@dataclass
class Snippet:
    id: str; version: str; source: str; linked_node: Optional[str] = None


@dataclass
class Bundle:
    bundle_id: str; review_version: str; fields: dict; requirements: Requirements
    approvals: list; spec: Optional[Spec]; workflow: Optional[Workflow]; snippets: list; review_config: dict


def _get(d, k, typ, path, errs, required=True, default=None):
    if not isinstance(d, dict) or k not in d or d[k] is None:
        if required:
            errs.append(f"{path}.{k}: required")
        return default
    v = d[k]
    if not isinstance(v, typ) or (isinstance(v, bool) and typ is not bool):
        errs.append(f"{path}.{k}: expected {typ}, got {type(v).__name__}")
        return default
    return v


def _unique(ids, path, errs):
    seen = set()
    for i in ids:
        if i in seen:
            errs.append(f"{path}: duplicate id {i!r}")
        seen.add(i)


def parse_bundle(d):
    errs = []
    if not isinstance(d, dict):
        raise SchemaError(["bundle must be an object"])
    bid = _get(d, "bundle_id", str, "$", errs)
    rv = _get(d, "review_version", str, "$", errs)
    fields = {}
    for name, fd in (_get(d, "fields", dict, "$", errs, default={}) or {}).items():
        p = f"$.fields.{name}"
        t = _get(fd, "type", str, p, errs)
        if t not in ("number", "bool", "categorical"):
            errs.append(f"{p}.type: must be number|bool|categorical")
            continue
        lo, hi = fd.get("min"), fd.get("max")
        fields[name] = FieldDef(name, t, fd.get("unit"), bool(fd.get("integer", False)),
                                Fraction(str(lo)) if lo is not None else None, Fraction(str(hi)) if hi is not None else None)
    rq = d.get("requirements")
    reqs = None
    if isinstance(rq, dict):
        items = []
        for i, it in enumerate(rq.get("items") or []):
            p = f"$.requirements.items[{i}]"
            items.append(Req(_get(it, "id", str, p, errs, default="?"), _get(it, "anchor", str, p, errs, default="?"),
                             _get(it, "order", (int, float), p, errs, default=0), _get(it, "text", str, p, errs, default=""),
                             _get(it, "note", str, p, errs, required=False, default="")))
        _unique([x.id for x in items], "$.requirements.items", errs)
        reqs = Requirements(_get(rq, "id", str, "$.requirements", errs, default="?"),
                            _get(rq, "version", str, "$.requirements", errs, default="?"),
                            _get(rq, "units", dict, "$.requirements", errs, required=False, default={}) or {}, items)
    else:
        errs.append("$.requirements: required")
    approvals = []
    for i, a in enumerate(d.get("approvals") or []):
        p = f"$.approvals[{i}]"
        st = _get(a, "status", str, p, errs, default="")
        if st not in STATUSES_OK:
            errs.append(f"{p}.status: must be one of {sorted(STATUSES_OK)}")
        at = a.get("applies_to", "*") if isinstance(a, dict) else "*"
        if at != "*" and not (isinstance(at, list) and all(isinstance(x, str) for x in at)):
            errs.append(f"{p}.applies_to: must be '*' or a list of versions")
        approvals.append(Approval(_get(a, "id", str, p, errs, default="?"), _get(a, "target", str, p, errs, default="?"),
                                  st, _get(a, "seq", int, p, errs, default=0), at, a.get("new_when"), a.get("new_then"),
                                  list(a.get("supersedes") or []), a.get("units") or {}))
        if isinstance(a, dict) and st != "revoked" and a.get("new_when") is None and a.get("new_then") is None:
            errs.append(f"{p}: needs new_when and/or new_then")
    _unique([a.id for a in approvals], "$.approvals", errs)
    spec = None
    if isinstance(d.get("spec"), dict):
        s = d["spec"]
        rules = [SpecRule(_get(r, "id", str, f"$.spec.rules[{i}]", errs, default="?"), _get(r, "when", str, f"$.spec.rules[{i}]", errs, default=""),
                          _get(r, "then", str, f"$.spec.rules[{i}]", errs, default=""), r.get("source_ref") if isinstance(r, dict) else None)
                 for i, r in enumerate(s.get("rules") or [])]
        _unique([r.id for r in rules], "$.spec.rules", errs)
        spec = Spec(_get(s, "id", str, "$.spec", errs, default="spec"), _get(s, "version", str, "$.spec", errs, default="?"),
                    s.get("units") or {}, rules, s.get("default"))
    wf = None
    if isinstance(d.get("workflow"), dict):
        w = d["workflow"]
        nodes = []
        for i, n in enumerate(w.get("nodes") or []):
            p = f"$.workflow.nodes[{i}]"
            kind = _get(n, "kind", str, p, errs, default="")
            if kind not in ("decision", "terminal"):
                errs.append(f"{p}.kind: must be decision|terminal")
            if kind == "terminal" and not isinstance(n.get("route"), str):
                errs.append(f"{p}.route: required for terminal")
            br = []
            for j, b in enumerate(n.get("branches") or []):
                br.append((_get(b, "when", str, f"{p}.branches[{j}]", errs, default=""), _get(b, "to", str, f"{p}.branches[{j}]", errs, default="")))
            nodes.append(Node(_get(n, "id", str, p, errs, default="?"), kind, n.get("version"), br, n.get("route")))
        _unique([n.id for n in nodes], "$.workflow.nodes", errs)
        wf = Workflow(_get(w, "id", str, "$.workflow", errs, default="workflow"), _get(w, "version", str, "$.workflow", errs, default="?"),
                      w.get("units") or {}, _get(w, "entry", str, "$.workflow", errs, default=""), nodes)
    snips = []
    for i, s in enumerate(d.get("snippets") or []):
        p = f"$.snippets[{i}]"
        snips.append(Snippet(_get(s, "id", str, p, errs, default="?"), _get(s, "version", str, p, errs, default="?"),
                             _get(s, "source", str, p, errs, default=""), s.get("linked_node") if isinstance(s, dict) else None))
    _unique([s.id for s in snips], "$.snippets", errs)
    if errs:
        raise SchemaError(errs)
    return Bundle(bid, rv, fields, reqs, approvals, spec, wf, snips, d.get("review_config") or {})
