"""Review pipeline: parse -> effective requirements -> grid equivalence -> classify -> model advice -> status."""
import copy
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from fractions import Fraction

from . import effective as eff
from .equiv import BudgetExceeded, build_grid, explore, random_cells, sampled
from .ir import (CYCLE, ERROR_ROUTE, NO_ROUTE, OTHER, CondRouter, DecisionList, DLRouter, Rule, WFRouter, And,
                 canon, constants_of, fields_of, show)
from .lang import Unsupported, norm_route, parse_condition
from .model import advisor
from .pysubset import lift_python
from .schemas import SchemaError, parse_bundle

REVIEWER_VERSION = "1.2.0"
SCOPE = ("Supported scope: requirement/spec/workflow conditions in the documented condition language over declared "
         "number/bool/categorical fields (missing values, inclusive/exclusive bounds, equality, nested AND/OR/NOT, declared "
         "units, ordered rules, branch-order workflows) and Python snippets in the restricted `route(ticket)` subset "
         "(lifted by ast, never executed). Anything outside it is listed under coverage as unsupported / unreviewed.")
REF_INDEPENDENT = {"missing_branch", "runtime_error", "workflow_cycle", "dangling_branch", "syntax_error"}
CONFIG_WHITELIST = {"grid_cap", "require_model"}


@dataclass
class Config:
    grid_cap: int = 1500000
    mode: str = "exhaustive"          # "random" = ABLATION: uniform random tickets instead of the partition grid
    sample_seed: int = 1234
    random_samples: int = 40
    apply_approvals: bool = True      # False = ABLATION
    prune: bool = True                # False = ABLATION (exact either way; only cost changes)
    max_input_chars: int = 500000
    require_model: bool = False
    model_max_calls: int = 3
    model_max_tokens: int = 20000
    model_response_tokens: int = 1500
    model_temperature: float = 0.0
    model_seed: int = 0
    model_timeout_s: float = 30.0
    max_prompt_chars: int = 60000
    time_budget_s: float = 30.0        # wall-clock budget; exceeding it degrades to the labelled SAMPLED mode (never PASS)
    sample_cap: int = 5000             # cells evaluated in sampled fallback (also capped by grid_cap)


def _jv(v):
    if isinstance(v, Fraction):
        return int(v) if v.denominator == 1 else float(v)
    return v


def _hash(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()


def decide(findings, incomplete):
    """Pure status function."""
    if any(f["severity"] == "blocker" and f.get("evidence_verified") for f in findings):
        return "CHANGES_REQUIRED"
    return "INCOMPLETE" if incomplete else "PASS"


def review(bundle_dict, cfg=None, client=None, cache=None):
    cfg = copy.copy(cfg) if cfg else Config()
    cfg_notes = []
    if isinstance(bundle_dict, dict) and isinstance(bundle_dict.get("review_config"), dict):
        # review_config comes from the (untrusted) bundle: it may only TIGHTEN operator policy, never relax it.
        for k, v in bundle_dict["review_config"].items():
            if k not in CONFIG_WHITELIST:
                cfg_notes.append(f"review_config.{k} ignored: not a whitelisted key")
            elif k == "require_model":
                if v is True:
                    cfg.require_model = True
                elif cfg.require_model:
                    cfg_notes.append("review_config.require_model=%r ignored: a bundle cannot relax operator policy" % (v,))
            elif k == "grid_cap":
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    if v < cfg.grid_cap:
                        cfg.grid_cap = v
                    else:
                        cfg_notes.append(f"review_config.grid_cap={v} ignored: a bundle cannot raise the operator cap {cfg.grid_cap}")
                else:
                    cfg_notes.append(f"review_config.grid_cap={v!r} ignored: must be a positive integer")
    key = _hash({"bundle": bundle_dict, "cfg": asdict(cfg), "v": REVIEWER_VERSION,
                 "client": [getattr(client, "name", None), getattr(client, "model", None), getattr(client, "script", None)]})
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            hit = copy.deepcopy(hit)
            hit["cache"] = {"hit": True, "key": key}
            return hit
    t0 = time.perf_counter()
    try:
        rep = _review(bundle_dict, cfg, client)
    except Exception as e:      # malformed input must never crash the reviewer; it is INCOMPLETE, never PASS
        bid = bundle_dict.get("bundle_id") if isinstance(bundle_dict, dict) else None
        reason = f"internal error while reviewing (likely malformed input; nothing was reviewed): {type(e).__name__}: {str(e)[:200]}"
        rep = _early(bid if isinstance(bid, (str, int)) else None, "INCOMPLETE", reason,
                     "The bundle could not be processed; no semantic review was possible.")
        rep["internal_error"] = {"type": type(e).__name__, "message": str(e)[:200]}
    if isinstance(rep.get("coverage"), dict):
        rep["coverage"]["config_notes"] = cfg_notes
    rep["provenance"] = {"reviewer": "final", "reviewer_version": REVIEWER_VERSION, "config": asdict(cfg),
                         "input_sha256": _hash(bundle_dict), "cache_key": key, "runtime_s": round(time.perf_counter() - t0, 5),
                         "python": sys.version.split()[0]}
    rep["cache"] = {"hit": False, "key": key}
    if cache is not None:
        cache.put(key, rep)
    return rep


PRIMARY_CATS = {"changed_threshold", "boundary_mismatch", "unit_mismatch", "changed_condition", "wrong_destination",
                "omitted_requirement", "missing_branch", "runtime_error", "workflow_cycle", "unexpected_rule", "default_route_mismatch"}


def _brief(f):
    ce = f.get("counterexample") or {}
    return {"id": f["id"], "category": f["category"], "rule_ref": f["rule_ref"], "location": f["location"],
            "ticket": ce.get("ticket"), "expected_route": ce.get("expected_route"), "actual_route": ce.get("actual_route")}


def consolidate(F):
    """Group behavioural findings by root cause WITHOUT dropping any information or changing severity semantics.
    (1) findings on the same (artifact, requirement) with a primary category are merged into the first one;
    (2) a `precedence_error` raised at the same artifact location as a primary finding is a derived effect of it.
    Merged findings are kept under `related_findings` (with their own tickets). Only findings of equal severity are merged,
    so a confirmed blocker is never absorbed into (or hidden behind) an unconfirmed warning."""
    beh = [f for f in F if f.get("check_mode") and f["source"] == "deterministic"]
    absorbed = set()
    prim = [f for f in beh if f["category"] in PRIMARY_CATS]
    # union primary findings that share (artifact, severity) AND (the same requirement OR the same artifact location)
    parent = {f["id"]: f["id"] for f in prim}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, f in enumerate(prim):
        for g in prim[:i]:
            if f["artifact"] == g["artifact"] and f["severity"] == g["severity"] and (
                    (f["rule_ref"] is not None and f["rule_ref"] == g["rule_ref"]) or f["location"] == g["location"]):
                parent[find(f["id"])] = find(g["id"])
    hosts = {}
    for f in prim:
        root = find(f["id"])
        if root in hosts:
            hosts[root].setdefault("related_findings", []).append(_brief(f))
            absorbed.add(f["id"])
        else:
            hosts[root] = f
    kept_prim = [f for f in prim if f["id"] not in absorbed]
    for f in beh:
        if f["category"] != "precedence_error":
            continue
        host = next((p for p in kept_prim if p["artifact"] == f["artifact"] and p["location"] == f["location"]
                     and p["severity"] == f["severity"]), None)
        if host is not None:
            host.setdefault("related_findings", []).append({**_brief(f), "derived_effect": True})
            absorbed.add(f["id"])
    out = [f for f in F if f["id"] not in absorbed]
    for f in out:
        if f.get("related_findings"):
            f["root_cause_group_size"] = 1 + len(f["related_findings"])
    for i, f in enumerate(out, 1):
        f["id"] = f"F{i}"
    return out


def _empty_cov():
    return {"scope": SCOPE, "requirements": [], "artifacts": {}, "approvals": {}, "untrusted_content": [], "unreviewed": []}


def _early(bid, status, reason, summary):
    return {"bundle_id": bid, "status": status, "summary": summary, "findings": [], "coverage": {**_empty_cov(), "unreviewed": [reason]},
            "checks": [], "model": None, "incomplete_reasons": [reason]}


# ------------------------------------------------------------------ parsing of artifacts
def _parse_spec(spec, fields):
    rules, uns, default = [], [], None
    for i, r in enumerate(spec.rules):
        loc = f"rules[{i}] (id={r.id})"
        try:
            rules.append(Rule(r.id, parse_condition(r.when, fields, spec.units), norm_route(r.then), loc, f"{r.when} -> {r.then}"))
        except Unsupported as u:
            uns.append({"location": loc, "text": f"{r.when} -> {r.then}", "reason": u.reason})
    if spec.default is not None:
        try:
            default = norm_route(spec.default)
        except Unsupported as u:
            uns.append({"location": "default", "text": spec.default, "reason": u.reason})
    return DecisionList(tuple(rules), default), uns


def _parse_workflow(wf, fields):
    nodes, uns, pseudo = {}, [], []
    for n in wf.nodes:
        if n.kind == "terminal":
            try:
                nodes[n.id] = {"kind": "terminal", "route": norm_route(n.route)}
            except Unsupported as u:
                uns.append({"location": f"nodes[{n.id}]", "text": str(n.route), "reason": u.reason})
            continue
        br = []
        for j, (when, to) in enumerate(n.branches):
            try:
                br.append((parse_condition(when, fields, wf.units), to))
            except Unsupported as u:
                uns.append({"location": f"nodes[{n.id}].branches[{j}]", "text": when, "reason": u.reason})
        nodes[n.id] = {"kind": "decision", "branches": br}
    for n in wf.nodes:                                   # pseudo-rules used for rule-level classification
        if n.kind != "decision":
            continue
        for j, (when, to) in enumerate(n.branches):
            t = nodes.get(to)
            if t and t["kind"] == "terminal" and nodes[n.id]["kind"] == "decision" and j < len(nodes[n.id]["branches"]):
                pseudo.append(Rule(f"{n.id}[{j}]", nodes[n.id]["branches"][j][0], t["route"], f"nodes[{n.id}].branches[{j}]", f"{when} -> {to}"))
    return nodes, uns, pseudo


def _reachable(entry, nodes):
    seen, stack = set(), [entry]
    while stack:
        n = stack.pop()
        if n in seen or n not in nodes:
            continue
        seen.add(n)
        stack += [to for _, to in nodes[n].get("branches", [])]
    return seen


# ------------------------------------------------------------------ main
def _review(raw, cfg, client):
    t_start = time.monotonic()
    t_main, t_all = t_start + 0.6 * cfg.time_budget_s, t_start + cfg.time_budget_s
    if len(json.dumps(raw, default=str)) > cfg.max_input_chars:
        return _early(raw.get("bundle_id") if isinstance(raw, dict) else None, "INCOMPLETE",
                      f"input exceeds max_input_chars={cfg.max_input_chars}; nothing was truncated, nothing was reviewed",
                      "Input too large to review within configured limits.")
    try:
        b = parse_bundle(raw)
    except SchemaError as e:
        return _early(raw.get("bundle_id") if isinstance(raw, dict) else None, "INCOMPLETE",
                      "invalid bundle (format validity is not semantic correctness): " + "; ".join(e.errors[:5]),
                      "The bundle does not satisfy the input contract; no semantic review was possible.")
    F, inc, checks, cov = [], [], [], _empty_cov()
    counter = [0]

    def finding(severity, category, artifact, version, location, summary, evidence, correction, rule_ref=None, ce=None,
                verified=True, confirmed=True, mode=None, extra=None):
        counter[0] += 1
        f = {"id": f"F{counter[0]}", "severity": severity, "category": category, "rule_ref": rule_ref, "artifact": artifact,
             "version": version, "location": location, "summary": summary, "evidence": evidence, "counterexample": ce,
             "correction": correction, "source": "deterministic", "evidence_verified": verified, "confirmed": confirmed,
             "check_mode": mode}
        if extra:
            f.update(extra)
        F.append(f)
        return f

    # ---- 1. effective requirements
    E = eff.derive(b, cfg.apply_approvals)
    ref_complete = E.complete
    for u in E.unsupported:
        inc.append(f"requirement {u['id']} is outside the supported language: {u['reason']}")
        cov["unreviewed"].append(f"requirement {u['id']} ({u['anchor']}): {u['reason']}")
        finding("major", "unsupported_requirement", "requirements", b.requirements.version, u["anchor"],
                f"Requirement {u['id']} cannot be parsed, so it is not reviewed.", [{"artifact": "requirements", "version": b.requirements.version, "location": u["anchor"], "text": u["text"]}],
                "Rewrite the requirement in the supported pattern (IF <condition> THEN <route>) or review it manually.", u["id"], verified=True)
    for c in E.conflicts:
        inc.append(f"approval conflict on {c['target']}: {', '.join(c['approvals'])} ({c['reason']})")
        cov["unreviewed"].append(f"requirement {c['target']}: effective {c['attribute']} undetermined (approval conflict)")
        finding("major", "approval_conflict", "approvals", "-", f"approvals {', '.join(c['approvals'])}",
                f"Approved records for {c['target']} disagree ({c['values']}); no version was chosen.",
                [{"artifact": "approvals", "version": "-", "location": a, "text": str(c["values"])} for a in c["approvals"]],
                "Resolve with a record that lists the retired record in `supersedes`, or withdraw one record.", c["target"])
    for a in E.ambiguities:
        inc.append(f"approval/requirement ambiguity: {a['reason']} ({a.get('target')})")
        cov["unreviewed"].append(f"ambiguity: {a['reason']} ({a.get('target')})")
    for u in E.untrusted:
        cov["untrusted_content"].append(u)
        finding("info", "untrusted_content_ignored", u["artifact"], b.requirements.version, u["location"],
                f"Prose contains {', '.join(u['flags'])}; it has no authority and was ignored.",
                [{"artifact": u["artifact"], "version": b.requirements.version, "location": u["location"], "text": u["text"]}],
                "Record approvals as top-level approval records; remove instruction-like text from requirements.")

    # ---- 2. artifacts
    fields = b.fields
    routers, conds, art_rules, art_ver, art_cov = {}, [], {}, {}, {}
    ref_dl = E.dl()
    routers["ref"] = DLRouter("ref", ref_dl)
    conds += [r.cond for r in E.rules] + E.extra_conds
    cov["artifacts"]["requirements"] = {"version": b.requirements.version, "status": "reviewed" if ref_complete else "partially_reviewed"}

    def stale(name, ver):
        if ver != b.review_version:
            inc.append(f"{name} is version {ver!r} but review_version is {b.review_version!r}")
            cov["unreviewed"].append(f"{name}@{ver}: not the version under review")
            cov["artifacts"][name] = {"version": ver, "status": "not_reviewed", "reason": "version mismatch"}
            finding("major", "version_mismatch", name, ver, "artifact", f"{name} is at {ver}, review is for {b.review_version}.",
                    [{"artifact": name, "version": ver, "location": "artifact", "text": f"version={ver}"}], "Review the artifact version that matches the approvals in force.")
            return True
        return False

    if b.spec is None:
        inc.append("missing artifact: spec")
        cov["artifacts"]["spec"] = {"status": "not_reviewed", "reason": "absent"}
    elif not stale("spec", b.spec.version):
        dl, uns = _parse_spec(b.spec, fields)
        art_ver["spec"] = b.spec.version
        req_ids = {it.id for it in b.requirements.items}
        for i, sr in enumerate(b.spec.rules):
            if sr.source_ref is not None and sr.source_ref not in req_ids:
                finding("warning", "dangling_source_ref", "spec", b.spec.version, f"rules[{i}] (id={sr.id})",
                        f"Spec rule {sr.id} cites source_ref {sr.source_ref!r}, which is not a requirement id (mapping is checked behaviourally; this is a metadata defect).",
                        [{"artifact": "spec", "version": b.spec.version, "location": f"rules[{i}] (id={sr.id})", "text": f"source_ref={sr.source_ref}"}],
                        "Point source_ref at an existing requirement id or remove it.", None)
        if uns:
            cov["artifacts"]["spec"] = {"version": b.spec.version, "status": "not_reviewed", "unsupported": uns}
            for u in uns:
                inc.append(f"spec {u['location']} unsupported: {u['reason']}")
                cov["unreviewed"].append(f"spec {u['location']}: {u['reason']}")
        else:
            routers["spec"], art_rules["spec"] = DLRouter("spec", dl), list(dl.rules)
            conds += [r.cond for r in dl.rules]
            cov["artifacts"]["spec"] = {"version": b.spec.version, "status": "reviewed", "unsupported": []}
    wf_nodes = None
    if b.workflow is None:
        inc.append("missing artifact: workflow")
        cov["artifacts"]["workflow"] = {"status": "not_reviewed", "reason": "absent"}
    elif not stale("workflow", b.workflow.version):
        wf_nodes, uns, pseudo = _parse_workflow(b.workflow, fields)
        art_ver["workflow"] = b.workflow.version
        for n in b.workflow.nodes:
            if n.version and n.version != b.workflow.version:
                inc.append(f"workflow node {n.id} is version {n.version!r}, workflow is {b.workflow.version!r}")
        if b.workflow.entry not in wf_nodes:
            finding("blocker", "dangling_branch", "workflow", b.workflow.version, "entry", f"Entry node {b.workflow.entry!r} does not exist.",
                    [{"artifact": "workflow", "version": b.workflow.version, "location": "entry", "text": b.workflow.entry}], "Set `entry` to an existing node.")
        for n in b.workflow.nodes:
            for j, (when, to) in enumerate(n.branches):
                if to not in wf_nodes:
                    finding("blocker", "dangling_branch", "workflow", b.workflow.version, f"nodes[{n.id}].branches[{j}]",
                            f"Branch destination {to!r} does not exist.", [{"artifact": "workflow", "version": b.workflow.version, "location": f"nodes[{n.id}].branches[{j}]", "text": f"{when} -> {to}"}],
                            f"Point the branch at an existing node.")
        if uns:
            cov["artifacts"]["workflow"] = {"version": b.workflow.version, "status": "not_reviewed", "unsupported": uns}
            for u in uns:
                inc.append(f"workflow {u['location']} unsupported: {u['reason']}")
                cov["unreviewed"].append(f"workflow {u['location']}: {u['reason']}")
        else:
            reach = _reachable(b.workflow.entry, wf_nodes)
            for n in b.workflow.nodes:
                if n.id not in reach:
                    finding("warning", "unreachable_node", "workflow", b.workflow.version, f"nodes[{n.id}]", f"Node {n.id} is unreachable from the entry.",
                            [{"artifact": "workflow", "version": b.workflow.version, "location": f"nodes[{n.id}]", "text": n.kind}], "Remove it or connect it.")
            routers["workflow"], art_rules["workflow"] = WFRouter("workflow", b.workflow.entry, wf_nodes), pseudo
            conds += [c for n in wf_nodes.values() for c, _ in n.get("branches", [])]
            cov["artifacts"]["workflow"] = {"version": b.workflow.version, "status": "reviewed", "unsupported": [], "reachable_nodes": sorted(reach)}
    node_ids = {n.id for n in b.workflow.nodes} if b.workflow else set()
    code_info = {}
    for s in b.snippets:
        name = f"code:{s.id}"
        info = {"version": s.version, "linked_node": s.linked_node, "syntax_check": None, "inspection": None, "execution": "not_performed"}
        code_info[name] = info
        cov["artifacts"][name] = info
        if stale(name, s.version):
            info.update(syntax_check="not_run", inspection="not_run")
            continue
        art_ver[name] = s.version
        if s.linked_node and b.workflow and s.linked_node not in node_ids:
            finding("blocker", "dangling_link", name, s.version, "linked_node", f"Snippet links to unknown node {s.linked_node!r}.",
                    [{"artifact": name, "version": s.version, "location": "linked_node", "text": s.linked_node}], "Link the snippet to an existing node.")
        lr = lift_python(s.source, fields)
        for line, text in lr.comments:
            fl = eff.scan_untrusted(text)
            if fl:
                u = {"artifact": name, "location": f"line {line}", "text": text, "flags": fl, "effect": "ignored"}
                cov["untrusted_content"].append(u)
                finding("info", "untrusted_content_ignored", name, s.version, f"line {line}", f"Code comment contains {', '.join(fl)}; ignored.",
                        [{"artifact": name, "version": s.version, "location": f"line {line}", "text": text}], "Remove instruction-like comments.")
        if not lr.syntax_ok:
            info.update(syntax_check="failed", inspection="not_run", status="not_reviewed", syntax_error=lr.syntax_error)
            finding("blocker", "syntax_error", name, s.version, lr.syntax_error.split(":")[0], f"Snippet does not parse: {lr.syntax_error}.",
                    [{"artifact": name, "version": s.version, "location": lr.syntax_error.split(":")[0], "text": lr.syntax_error}], "Fix the syntax error.")
            continue
        info["syntax_check"] = "ok"
        if lr.unsupported:
            info.update(inspection="unsupported", status="not_reviewed", unsupported=[{"location": l, "reason": r} for l, r in lr.unsupported])
            for l, r in lr.unsupported:
                inc.append(f"{name} {l} outside restricted subset: {r}")
                cov["unreviewed"].append(f"{name} {l}: {r}")
            continue
        info.update(inspection="lifted", status="reviewed", unsupported=[])
        routers[name], art_rules[name] = DLRouter(name, lr.dl), list(lr.dl.rules)
        conds += [r.cond for r in lr.dl.rules]
    if not b.snippets:
        cov["artifacts"]["code"] = {"status": "not_supplied"}

    # ---- 3. equivalence over the partition grid
    reviewed = [n for n in routers if n != "ref"]
    grid = build_grid(fields, conds)
    res = None
    if reviewed:
        try:
            if cfg.mode == "random":
                res = sampled(routers, grid, cfg.random_samples, cfg.sample_seed, cells=random_cells(grid, cfg.random_samples, cfg.sample_seed),
                              note="ABLATION: uniform random tickets, partition disabled")
            else:
                res = explore(routers, grid, cfg.grid_cap, cfg.prune, deadline=t_main)
        except BudgetExceeded:
            res = sampled(routers, grid, min(cfg.grid_cap, cfg.sample_cap), cfg.sample_seed,
                          note=f"leaf cap {cfg.grid_cap} or time budget {cfg.time_budget_s}s exceeded; sampled fallback (boundary probes + pairwise + seeded random)")
    all_fields = list(fields)

    def complete(assign):
        t = {}
        for f in all_fields:
            fd = fields[f]
            if f in assign:
                t[f] = assign[f]
            elif f in grid.reps and any(v is not None for v in grid.reps[f]):
                t[f] = [v for v in grid.reps[f] if v is not None][0]
            else:
                t[f] = False if fd.type == "bool" else (OTHER if fd.type == "categorical" else (fd.lo if fd.lo is not None else Fraction(0)))
        return t

    cache_diff, degraded = {}, [False]

    def bounded(routers_, sub):
        try:
            return explore(routers_, sub, 10 ** 7, True, deadline=t_all)
        except BudgetExceeded:
            degraded[0] = True
            return None

    def cond_diff(c1, c2):
        k = (canon(c1).__repr__(), canon(c2).__repr__())
        if k not in cache_diff:
            sub = grid.subset(fields_of(c1) | fields_of(c2))
            r = bounded({"a": CondRouter("a", c1), "b": CondRouter("b", c2)}, sub)
            cache_diff[k] = 10 ** 9 if r is None else sum(g.covered for g in r.regions if g.outs["a"].route != g.outs["b"].route)
        return cache_diff[k]

    ref_by_id = {r.id: r for r in E.rules}
    ref_order = [r.id for r in E.rules]

    def sub_kind(r, x):
        cr, cx = constants_of(r.cond), constants_of(x.cond)
        sr, sx = {(f, v) for f, v in cr}, {(f, v) for f, v in cx}
        if sr == sx and cond_diff(r.cond, x.cond):
            return "boundary_mismatch"
        fr, fx = {}, {}
        for f, v in cr:
            fr.setdefault(f, set()).add(v)
        for f, v in cx:
            fx.setdefault(f, set()).add(v)
        for f in fr.keys() & fx.keys():
            if fr[f] != fx[f]:
                if len(fr[f]) == 1 and len(fx[f]) == 1:
                    a, c = list(fr[f])[0], list(fx[f])[0]
                    if isinstance(a, Fraction) and isinstance(c, Fraction) and a and c and (a / c) in (60, Fraction(1, 60), 3600, Fraction(1, 3600)):
                        return "unit_mismatch"
                return "changed_threshold"
        return "changed_condition"

    def nearest(r, cands):
        return min(cands, key=lambda x: cond_diff(r.cond, x.cond))

    def classify(name, ro, ao, rules_of_art):
        """-> dict(category, ref_rule, art_rule, detail) from isolated rule matching + the mismatch pattern."""
        if ao.route == NO_ROUTE:
            return {"category": "missing_branch", "ref_rule": ref_by_id.get(ro.trace[0]), "art_rule": None}
        if ao.route == ERROR_ROUTE:
            return {"category": "runtime_error", "ref_rule": ref_by_id.get(ro.trace[0]), "art_rule": next((x for x in rules_of_art if x.id == ao.trace[0]), None)}
        if ao.route == CYCLE:
            return {"category": "workflow_cycle", "ref_rule": None, "art_rule": None}
        if ao.route.startswith("<dangling"):
            return {"category": "dangling_branch", "ref_rule": None, "art_rule": None}
        by_id = {x.id: x for x in rules_of_art}
        dec = [t for t in ao.trace if "[" in t][-1] if name == "workflow" and any("[" in t for t in ao.trace) else (ao.trace[0] if ao.trace else "")
        s = by_id.get(dec)
        r = ref_by_id.get(ro.trace[0])
        if r is not None:
            exact = [x for x in rules_of_art if x.route == r.route and cond_diff(r.cond, x.cond) == 0]
            if exact:
                return {"category": "precedence_error", "ref_rule": r, "art_rule": s or exact[0], "exact": exact[0]}
            wrong = [x for x in rules_of_art if x.route != r.route and cond_diff(r.cond, x.cond) == 0]
            if wrong:
                return {"category": "wrong_destination", "ref_rule": r, "art_rule": wrong[0]}
            cands = [x for x in rules_of_art if x.route == r.route]
            if not cands:
                return {"category": "omitted_requirement", "ref_rule": r, "art_rule": s}
            best = nearest(r, cands)
            return {"category": sub_kind(r, best), "ref_rule": r, "art_rule": best}
        if s is not None:
            cands = [y for y in E.rules if y.route == s.route]
            if cands:
                y = min(cands, key=lambda y: cond_diff(y.cond, s.cond))
                return {"category": sub_kind(y, s), "ref_rule": y, "art_rule": s}
            return {"category": "unexpected_rule", "ref_rule": None, "art_rule": s}
        return {"category": "default_route_mismatch", "ref_rule": None, "art_rule": None}

    groups = {}
    if res is not None:
        for reg in res.regions:
            ro = reg.outs["ref"]
            for name in reviewed:
                ao = reg.outs[name]
                if ao.route != ro.route:
                    dec = [t for t in ao.trace if "[" in t]
                    key = (name, ro.trace[0] if ro.trace else "", (dec[-1] if dec else (ao.trace[0] if ao.trace else "")), ro.route, ao.route)
                    g = groups.setdefault(key, {"cells": 0, "reg": reg, "ro": ro, "ao": ao})
                    g["cells"] += reg.covered
    mode = res.mode if res else "not_run"
    dedup = {}
    verification_failed = False
    for key, g in groups.items():
        name = key[0]
        cl = classify(name, g["ro"], g["ao"], art_rules.get(name, []))
        if cl["category"] == "dangling_branch" and name == "workflow":
            continue                                     # already reported structurally
        dk = (name, cl["category"], cl["ref_rule"].id if cl["ref_rule"] else None,
              None if cl["category"] == "omitted_requirement" else (cl["art_rule"].id if cl["art_rule"] else None))
        if dk in dedup:
            dedup[dk]["cells"] += g["cells"]
            continue
        dedup[dk] = {"cl": cl, "g": g, "cells": g["cells"], "name": name}
    for dk, d in dedup.items():
        cl, g, name = d["cl"], d["g"], d["name"]
        ticket = complete(g["reg"].assign)
        t_ref, t_art = routers["ref"].route3(ticket), routers[name].route3(ticket)
        if t_ref is None or t_art is None or t_ref.route != g["ro"].route or t_art.route != g["ao"].route:
            verification_failed = True
            inc.append("internal verification failure: counterexample did not reproduce under full evaluation")
            continue
        ce = {"ticket": {k: _jv(v) for k, v in ticket.items()}, "expected_route": t_ref.route, "actual_route": t_art.route,
              "expected_trace": list(t_ref.trace), "actual_trace": list(t_art.trace), "cells_affected": d["cells"],
              "of_cells": res.cells_total, "mode": mode, "verified_by_reevaluation": True,
              "note": "waiting is in minutes" if "waiting" in fields else ""}
        r, x, cat = cl["ref_rule"], cl["art_rule"], cl["category"]
        ev = []
        if r is not None:
            ap = next((a for a in E.applied if a["requirement"] == r.id), None)
            ev.append({"artifact": "requirements", "version": b.requirements.version, "location": r.anchor,
                       "text": f"{r.base_text}" + (f"  [effective after approvals {ap['approvals']}: {r.text}]" if ap else f"  [effective: {r.text}]")})
        elif E.default_id:
            ev.append({"artifact": "requirements", "version": b.requirements.version, "location": E.default_anchor, "text": f"default -> {E.default}"})
        if x is not None:
            ev.append({"artifact": name, "version": art_ver[name], "location": x.loc, "text": x.text})
        if cl.get("exact") is not None and cl["exact"] is not x:
            ev.append({"artifact": name, "version": art_ver[name], "location": cl["exact"].loc, "text": cl["exact"].text + "  (equivalent rule, but shadowed by the rule above)"})
        if not ev or ev[-1]["artifact"] != name:
            ev.append({"artifact": name, "version": art_ver[name], "location": "no rule/branch applies" if cat in ("missing_branch", "omitted_requirement") else "-", "text": f"route={t_art.route} trace={list(t_art.trace)}"})
        loc = x.loc if x is not None else (f"nodes[{t_art.trace[-1].split('[')[0]}]" if t_art.trace and name == "workflow" else (next((e["location"] for e in ev if e["artifact"] == name), "-")))
        if cat in ("missing_branch",) and name == "workflow":
            loc = f"nodes[{t_art.trace[-1].split('[')[0]}]"
        if cat == "omitted_requirement":
            loc = r.anchor if r else loc
            ev[-1]["text"] = f"no {name} rule routes to {r.route!r} under this condition; fired {list(t_art.trace)}" if r else ev[-1]["text"]
        corr = {
            "changed_threshold": lambda: f"Change {loc} to '{r.text.split(' -> ')[0]}' (effective requirement {r.id}; numbers in canonical units).",
            "boundary_mismatch": lambda: f"Use the inclusive/exclusive bound of requirement {r.id}: '{r.text.split(' -> ')[0]}'.",
            "unit_mismatch": lambda: f"Declare the unit explicitly: requirement {r.id} means '{r.text.split(' -> ')[0]}' in canonical units.",
            "changed_condition": lambda: f"Make {loc} equivalent to requirement {r.id}: '{r.text.split(' -> ')[0]}'.",
            "omitted_requirement": lambda: f"Add a rule for requirement {r.id} ('{r.text}') at its precedence position.",
            "precedence_error": lambda: f"Reorder so requirement order {ref_order} is respected: the rule at {loc} must not fire before {r.id}.",
            "wrong_destination": lambda: f"Route {loc} to '{r.route}' as required by {r.id}.",
            "missing_branch": lambda: f"Add an outgoing branch for this ticket (e.g. an else branch to '{t_ref.route}').",
            "runtime_error": lambda: "Guard the comparison with `is not None` or otherwise define behaviour for missing values.",
            "unexpected_rule": lambda: f"Remove {loc} or add a source requirement that justifies it.",
            "workflow_cycle": lambda: "Break the cycle in the workflow graph.",
        }.get(cat, lambda: "Align the artifact with the effective requirements.")()
        confirmed = ref_complete or cat in REF_INDEPENDENT
        sev = "blocker" if confirmed else "warning"
        finding(sev, cat, name, art_ver[name], loc,
                f"{name} routes the counterexample ticket to {t_art.route!r}; the effective requirements say {t_ref.route!r}."
                + ("" if confirmed else " Reference is incomplete, so this is unconfirmed."),
                ev, corr, rule_ref=(r.id if r else (E.default_id if cat in ("missing_branch", "runtime_error", "default_route_mismatch") else None)), ce=ce, verified=True, confirmed=confirmed, mode=mode)
    if res is not None:
        for name in reviewed:
            mism = [k for k in groups if k[0] == name]
            if mode == "exhaustive":
                result = "mismatch" if mism else "equivalent"
            else:
                result = "mismatch" if mism else "inconclusive"
                if not mism:
                    inc.append(f"{name}: equivalence only sampled ({mode}); sampled agreement cannot support PASS")
                    cov["unreviewed"].append(f"{name}: not exhaustively compared ({res.note or 'sampled'})")
            checks.append({"check": f"effective_requirements_vs_{name}", "artifact": name, "mode": mode, "result": result,
                           "cells_total": res.cells_total, "cells_evaluated": res.leaves, "cells_covered": res.cells_covered,
                           "cells_pruned_exact": (res.cells_covered - res.leaves) if mode == "exhaustive" else 0,
                           "cap": cfg.grid_cap, "seed": res.seed, "mismatch_groups": len([1 for k in groups if k[0] == name]),
                           "reference_complete": ref_complete, "note": res.note})
        if mode == "exhaustive":                             # interaction: dead rules / branches
            for name in ["ref"] + reviewed:
                rules_here = ([(r.id, r.anchor) for r in E.rules] if name == "ref" else [(r.id, r.loc) for r in art_rules.get(name, [])])
                if name == "workflow":
                    rules_here = [(f"{n}[{j}]", f"nodes[{n}].branches[{j}]") for n, nd in wf_nodes.items() for j, _ in enumerate(nd.get("branches", []))
                                  if n in _reachable(b.workflow.entry, wf_nodes)]
                for rid, loc in rules_here:
                    if rid not in res.fired[name]:
                        finding("warning", "shadowed_rule", "requirements" if name == "ref" else name,
                                b.requirements.version if name == "ref" else art_ver[name], loc,
                                f"Rule {rid} never fires for any ticket in the supported domain (dead / shadowed).",
                                [{"artifact": name, "version": art_ver.get(name, b.requirements.version), "location": loc, "text": rid}],
                                "Remove or reorder it; an unreachable rule is usually a precedence mistake.", rid if name == "ref" else None, mode=mode)
    # rule overlap on the effective requirements (interaction report)
    overlaps = []
    for i in range(len(E.rules)):
        for j in range(i + 1, len(E.rules)):
            a, c = E.rules[i], E.rules[j]
            sub = grid.subset(fields_of(a.cond) | fields_of(c.cond))
            r = bounded({"x": CondRouter("x", And((a.cond, c.cond)))}, sub)
            if r is None:
                continue
            hit = next((g for g in r.regions if g.outs["x"].route == "T"), None)
            if hit:
                overlaps.append({"rules": [a.id, c.id], "winner": a.id, "example_ticket": {k: _jv(v) for k, v in complete(hit.assign).items()},
                                 "note": "both conditions hold; earlier requirement wins by precedence"})
    checks.append({"check": "requirement_overlaps", "mode": "exhaustive" if not degraded[0] else "partial", "pairs": overlaps})
    if degraded[0]:
        cov["unreviewed"].append("per-rule classification / overlap detail truncated by the time budget (the status itself comes from the main comparison)")

    # ---- 4. coverage of requirements
    for it in sorted(b.requirements.items, key=lambda x: (x.order, x.id)):
        row = {"id": it.id, "anchor": it.anchor, "supported": it.id not in {u["id"] for u in E.unsupported}, "effective": None,
               "applied_approvals": next((a["approvals"] for a in E.applied if a["requirement"] == it.id), []), "per_artifact": {}}
        er = ref_by_id.get(it.id)
        row["effective"] = er.text if er else (f"default -> {E.default}" if it.id == E.default_id else None)
        for name in reviewed:
            if it.id in E.unresolved_ids or not row["supported"]:
                row["per_artifact"][name] = "not_reviewed"
            elif er is None:
                row["per_artifact"][name] = "changed" if any(k[0] == name and k[1] == "default" for k in groups) else "covered"
            else:
                rs = art_rules.get(name, [])
                if any(x.route == er.route and cond_diff(er.cond, x.cond) == 0 for x in rs):
                    row["per_artifact"][name] = "covered"
                elif any(x.route == er.route for x in rs):
                    row["per_artifact"][name] = "changed"
                else:
                    row["per_artifact"][name] = "omitted"
        cov["requirements"].append(row)
    cov["approvals"] = {"applied": E.applied, "ignored": E.ignored, "conflicts": E.conflicts, "ambiguities": E.ambiguities}
    for r in b.spec.rules if b.spec and "spec" in routers else []:
        pass

    # ---- 5. model advice (advisory only)
    evidence, k = [], 0

    def ev_add(art, ver, loc, text, unparsed=False):
        nonlocal k
        k += 1
        evidence.append({"id": f"E{k}", "artifact": art, "version": ver, "location": loc, "text": text, "unparsed": unparsed})

    for r in E.rules:
        ev_add("requirements", b.requirements.version, r.anchor, f"{r.id}: {r.base_text}  [effective: {r.text}]")
    for name, rs in art_rules.items():
        for r in rs:
            ev_add(name, art_ver[name], r.loc, r.text)
    for u in E.unsupported:                              # content the deterministic checker could NOT read (untrusted data)
        ev_add("requirements", b.requirements.version, u["anchor"], f"{u['id']}: {u['text']}", True)
    for name_, info_ in cov["artifacts"].items():
        for u in (info_.get("unsupported") or []) if isinstance(info_, dict) else []:
            if isinstance(u, dict) and "text" in u:
                ev_add(name_, info_.get("version"), u["location"], u["text"], True)
    for sn in b.snippets:
        if code_info.get(f"code:{sn.id}", {}).get("inspection") == "unsupported":
            ev_add(f"code:{sn.id}", sn.version, "whole snippet", sn.source, True)
    known = {r.id for r in E.rules} | ({E.default_id} if E.default_id else set()) | {it.id for it in b.requirements.items}
    known |= {r.id for rs in art_rules.values() for r in rs}
    known |= {n for n in node_ids} | {s.id for s in b.snippets}
    msec = advisor.advise(client, evidence, F, known, cfg)
    if client is None and cfg.require_model:
        inc.append("model review is required but no model client was configured")
    elif cfg.require_model and msec["status"] != "ok":
        inc.append(f"required model check did not complete: {msec['reason']}")
    if msec["status"] == "failed" and not cfg.require_model:
        cov["unreviewed"].append(f"model advisory review did not complete ({msec['reason']}); deterministic review is unaffected")
    if msec["status"] == "not_run" and msec["reason"] and client is not None:
        cov["unreviewed"].append(f"model advisory review not run: {msec['reason']}")
    det_by_ref = {}
    for f in F:
        if f["severity"] == "blocker" and f.get("rule_ref"):
            det_by_ref.setdefault(f["rule_ref"], []).append(f)
    ev_by_id = {e["id"]: e for e in evidence}
    for m in msec["accepted"]:
        if m["claim_type"] == "ok":
            continue
        hit = det_by_ref.get(m.get("rule_ref"))
        if hit:
            hit[0].setdefault("model_notes", []).append({"summary": m["summary"], "suggested_correction": m.get("correction", "")})
        else:
            finding("advisory", "model_suggestion", "model", "-", "n/a", m["summary"],
                    [{"artifact": ev_by_id[e]["artifact"], "version": ev_by_id[e]["version"], "location": ev_by_id[e]["location"], "text": ev_by_id[e]["text"]} for e in m.get("evidence_ids", [])],
                    m.get("correction", ""), rule_ref=m.get("rule_ref"), verified=False, confirmed=False,
                    extra={"source": "model", "claim_type": m["claim_type"]})
    msec["calls_detail_note"] = "see `calls` for request configuration and response provenance"

    # ---- 5b. root-cause consolidation: one defective artifact rule must not inflate the blocker count.
    F[:] = consolidate(F)

    # ---- 6. status
    status = decide(F, inc)
    blockers = [f for f in F if f["severity"] == "blocker" and f["evidence_verified"]]
    if status == "CHANGES_REQUIRED":
        summary = f"{len(blockers)} evidenced blocker(s) found ({', '.join(sorted({f['category'] for f in blockers}))})."
        if inc:
            summary += f" Also unreviewed/unresolved: {len(inc)} item(s)."
    elif status == "INCOMPLETE":
        summary = "No blocker was established, but the review is incomplete: " + "; ".join(inc[:3]) + ("; ..." if len(inc) > 3 else "")
    else:
        summary = "No blocker found; the effective requirements were completely reviewed within the declared supported scope (exhaustive partition-grid comparison)."
    return {"bundle_id": b.bundle_id, "review_version": b.review_version, "status": status, "summary": summary, "findings": F,
            "coverage": cov, "checks": checks, "model": msec, "incomplete_reasons": inc}
