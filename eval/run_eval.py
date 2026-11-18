"""Evaluate an implementation on the frozen bundle set.

  python -m eval.run_eval --impl v0|final [--split all|dev|holdout] [--ablation NAME] --out reports/x.json

Labels (labels/labels.json) and oracle (oracle/cases.py) are read ONLY here, never passed to the reviewer.
Counterexamples are verified against the independent oracle, not against the reviewer's own evaluator.
"""
import argparse
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "oracle"))

from cases import CASES  # noqa: E402  (oracle)

STATUSES = ["PASS", "CHANGES_REQUIRED", "INCOMPLETE"]


def load_bundles():
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "fixtures", "bundles", "*.json"))):
        with open(p) as f:
            b = json.load(f)
        out[b["bundle_id"]] = b
    return out


def load_script(cid):
    p = os.path.join(ROOT, "fixtures", "model_scripts", cid + ".json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def run_impl(impl, bundle, cid, ablation):
    t0 = time.perf_counter()
    if impl == "v0":
        from baseline_v0.reviewer_v0 import review_v0
        rep = review_v0(bundle)
    else:
        from reviewer.review import review, Config
        from reviewer.model.fake import FakeClient
        cfg = Config()
        if ablation == "random_sampling":
            cfg.mode = "random"
        elif ablation == "no_approvals":
            cfg.apply_approvals = False
        elif ablation == "no_pruning":
            cfg.prune = False
        script = load_script(cid)
        client = FakeClient(script if script is not None else [{"kind": "valid", "body": {"findings": [], "overall_opinion": "PASS"}}])
        rep = review(bundle, cfg, client)
    rep["_runtime_s"] = time.perf_counter() - t0
    return rep


def art_key(name):
    return "code" if name.startswith("code") else name


def oracle_check(cid, f):
    """Does the finding's counterexample agree with the independent oracle?"""
    ce = f.get("counterexample")
    if not ce or cid not in CASES:
        return None
    case = CASES[cid]
    t = ce["ticket"]
    exp = case["expected"](t)
    act = case["actual"].get(art_key(f["artifact"]), case["expected"])(t)
    exp = "<no_route>" if exp is None else exp
    act = "<no_route>" if act is None else act
    return exp != act and exp == ce["expected_route"] and act == ce["actual_route"]


def finding_matches(e, f):
    return (f.get("severity") == "blocker" and art_key(f.get("artifact", "")) == e["artifact"]
            and f.get("category") in e["categories"]
            and (e["rule_ref"] is None or f.get("rule_ref") == e["rule_ref"])
            and e["location_contains"] in str(f.get("location", "")))


def score(results, labels):
    s = {"valid": dict(n=0, pass_=0, false_block=0, incomplete=0),
         "defective": dict(n=0, changes=0, false_pass=0, incomplete=0),
         "incomplete": dict(n=0, correct=0, unsafe_pass=0, blocked=0)}
    conf = {}
    fq = dict(expected_defects=0, found_correct=0, with_verified_evidence=0, with_oracle_valid_counterexample=0,
              counterexamples_checked=0, unsupported_blockers=0, total_blockers=0, model_only_advisories=0)
    for cid, r in results.items():
        lab = labels[cid]
        typ, exp, act = lab["type"], lab["expected_status"], r["status"]
        conf.setdefault(exp, {}).setdefault(act, 0)
        conf[exp][act] += 1
        d = s[typ]
        d["n"] += 1
        if typ == "valid":
            d["pass_"] += act == "PASS"; d["false_block"] += act == "CHANGES_REQUIRED"; d["incomplete"] += act == "INCOMPLETE"
        elif typ == "defective":
            d["changes"] += act == "CHANGES_REQUIRED"; d["false_pass"] += act == "PASS"; d["incomplete"] += act == "INCOMPLETE"
        else:
            d["correct"] += act == "INCOMPLETE"; d["unsafe_pass"] += act == "PASS"; d["blocked"] += act == "CHANGES_REQUIRED"
        blockers = [f for f in r.get("findings", []) if f.get("severity") == "blocker"]
        fq["total_blockers"] += len(blockers)
        matched_ids = set()
        for e in lab["expected_findings"]:
            fq["expected_defects"] += 1
            m = [f for f in blockers if finding_matches(e, f)]
            if m:
                fq["found_correct"] += 1
                matched_ids.add(id(m[0]))
                fq["with_verified_evidence"] += bool(m[0].get("evidence")) and bool(m[0].get("evidence_verified"))
                ok = oracle_check(cid, m[0])
                if ok is not None:
                    fq["counterexamples_checked"] += 1
                    fq["with_oracle_valid_counterexample"] += ok
        for f in blockers:
            if id(f) in matched_ids:
                continue
            fq["unsupported_blockers"] += 1
        fq["model_only_advisories"] += sum(1 for f in r.get("findings", []) if f.get("source") == "model")
    res = {"calls": 0, "retries": 0, "runtime_s": round(sum(r["_runtime_s"] for r in results.values()), 4)}
    for r in results.values():
        m = r.get("model") or {}
        res["calls"] += m.get("calls_made", 0)
        res["retries"] += m.get("retries", 0)
    return {"decision": s, "confusion_expected_to_actual": conf, "finding_quality": fq, "resource": res}


def main():
    # Refuse to evaluate if frozen fixtures, labels, split, generator provenance, or baseline changed.
    from eval.frozen_inputs import verify
    verify()
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", default="final")
    ap.add_argument("--split", default="all", choices=["all", "dev", "holdout"])
    ap.add_argument("--ablation", default="none")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    with open(os.path.join(ROOT, "labels", "labels.json")) as f:
        L = json.load(f)
    labels, holdout = L["labels"], set(L["holdout"])
    bundles = load_bundles()
    ids = [c for c in bundles if (a.split == "all" or (a.split == "holdout") == (c in holdout))]
    results = {c: run_impl(a.impl, bundles[c], c, a.ablation) for c in ids}
    m = score(results, labels)
    per_case = {}
    for c, r in results.items():
        per_case[c] = {"expected": labels[c]["expected_status"], "actual": r["status"],
                       "correct": labels[c]["expected_status"] == r["status"], "holdout": c in holdout,
                       "categories": sorted({f.get("category") for f in r.get("findings", []) if f.get("severity") == "blocker"})}
    out = {"impl": a.impl, "split": a.split, "ablation": a.ablation, "client": "fake (scripted, offline)",
           "n_cases": len(ids), "metrics": m, "per_case": per_case}
    if a.out:
        os.makedirs(os.path.dirname(os.path.join(ROOT, a.out)), exist_ok=True)
        with open(os.path.join(ROOT, a.out), "w") as f:
            json.dump(out, f, indent=1)
    d = m["decision"]
    print(f"[{a.impl} split={a.split} ablation={a.ablation}] n={len(ids)}")
    print(f"  valid:      {d['valid']['pass_']}/{d['valid']['n']} PASS, false blocks {d['valid']['false_block']}, abstained {d['valid']['incomplete']}")
    print(f"  defective:  {d['defective']['changes']}/{d['defective']['n']} blocked, false passes {d['defective']['false_pass']}, abstained {d['defective']['incomplete']}")
    print(f"  incomplete: {d['incomplete']['correct']}/{d['incomplete']['n']} correct, unsafe PASS {d['incomplete']['unsafe_pass']}, blocked {d['incomplete']['blocked']}")
    q = m["finding_quality"]
    print(f"  findings:   {q['found_correct']}/{q['expected_defects']} correct defect+location, oracle-valid counterexamples {q['with_oracle_valid_counterexample']}/{q['counterexamples_checked']}, unsupported blockers {q['unsupported_blockers']}/{q['total_blockers']}")
    print(f"  resource:   {m['resource']}")
    wrong = [c for c, p in per_case.items() if not p["correct"]]
    print("  wrong:", wrong)
    return out


if __name__ == "__main__":
    main()
