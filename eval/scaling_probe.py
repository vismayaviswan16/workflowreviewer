"""Measured scaling limit of the exhaustive partition-grid check (reported as a limitation, not hidden).
Policy: k extra independent numeric fields, each with one rule; spec and workflow are byte-for-byte equivalent to the requirements,
so the correct answer is always PASS. Reports runtime, comparison mode and status per k.
  python -m eval.scaling_probe --max-k 6 --out reports/scaling.json"""
import argparse, json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from eval.fuzz_differential import bundle
from reviewer.review import Config, review

def make(k):
    base = [(("cmp", "urgent", "==", True), "priority_queue"), (("cmp", "waiting", ">=", 30), "escalation")]
    extra = [(("cmp", f"f{i}", ">=", 10 * (i + 1)), "manual_review") for i in range(k)]
    rules = base + extra
    b = bundle(rules, rules, 0)
    for i in range(k):
        b["fields"][f"f{i}"] = {"type": "number"}
    return b

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--max-k", type=int, default=6); ap.add_argument("--out"); a = ap.parse_args()
    rows = []
    for k in range(a.max_k + 1):
        cfg = Config(); cfg.time_budget_s = 20.0
        t = time.perf_counter(); r = review(make(k), cfg); dt = time.perf_counter() - t
        chk = r["checks"][0] if r["checks"] else {}
        rows.append({"extra_numeric_fields": k, "status": r["status"], "mode": chk.get("mode"), "cells_total": chk.get("cells_total"),
                     "cells_evaluated": chk.get("cells_evaluated"), "runtime_s": round(dt, 3), "expected_status": "PASS"})
        print(rows[-1], flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"config": {"grid_cap": Config().grid_cap, "time_budget_s": 20.0}, "rows": rows}, fh, indent=1)

if __name__ == "__main__":
    main()
