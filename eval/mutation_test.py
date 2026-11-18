"""Mutation test of the REVIEWER itself (DELIBERATE FAULT INJECTION -- every mutant below is an injected fault, not a real
historical bug). Each mutant is applied to a temporary copy of the repo; it is KILLED if the unit tests fail or the final
benchmark has any wrong case. Survivors are reported, not hidden.
  python -m eval.mutation_test --out reports/mutation_test.json"""
import argparse, json, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (id, description, file, old, new)
M = [
 ("M01", "proposed (unapproved) approvals are honoured", "reviewer/effective.py", 'if a.status != "approved":', 'if a.status == "rejected":'),
 ("M02", "approval scope ignored", "reviewer/effective.py", 'bundle.review_version not in a.applies_to:\n            E.ignored', 'False:\n            E.ignored'),
 ("M03", "conflicting approvals not detected", "reviewer/effective.py", 'if len(d) > 1:', 'if len(d) > 99:'),
 ("M04", "INCOMPLETE never reported (always PASS)", "reviewer/review.py", 'return "INCOMPLETE" if incomplete else "PASS"', 'return "PASS"'),
 ("M05", "version mismatch ignored", "reviewer/review.py", 'if ver != b.review_version:', 'if False:'),
 ("M06", "minutes treated as seconds", "reviewer/lang.py", '"min": 60,', '"min": 1,'),
 ("M07", "None != c not modelled in lifted Python", "reviewer/pysubset.py", 'return Or((IsMissing(f), Cmp(f, "!=", lit)))      # Python: None != c is True', 'return Cmp(f, "!=", lit)'),
 ("M08", "unguarded ordering on None no longer a crash", "reviewer/pysubset.py", 'strict=f not in safe', 'strict=False'),
 ("M09", "invented model rule refs accepted", "reviewer/model/advisor.py", 'f["rule_ref"] not in known_refs', 'f["rule_ref"] is None'),
 ("M10", "cache key ignores bundle content (stale PASS)", "reviewer/review.py", 'key = _hash({"bundle": bundle_dict,', 'key = _hash({"bundle": 0,'),
 ("M11", "revocation event ignored", "reviewer/effective.py", 'retracted[sid] = rv.id', 'pass'),
 ("M12", "sampled agreement allowed to count as equivalence", "reviewer/review.py", 'if not mism:\n                    inc.append', 'if False:\n                    inc.append'),
 ("M13", "`not in` lifted as `in`", "reviewer/pysubset.py", 'return c if isinstance(op, ast.In) else Not(c)', 'return c'),
 ("M14", "blocker status requires no verification", "reviewer/review.py", 'f["severity"] == "blocker" and f.get("evidence_verified")', 'False'),
 ("M15", "config in bundle may relax operator policy", "reviewer/review.py", 'if v is True:\n                    cfg.require_model = True', 'cfg.require_model = bool(v)\n                if False:\n                    pass'),
 ("M16", "ternary else-branch dropped", "reviewer/pysubset.py", 'for x, r in self.expr(n.orelse, safe):\n                nc = Not(c)', 'for x, r in []:\n                nc = Not(c)'),
]

def run(cmd, cwd):
    # the implementation pin is deliberately relaxed inside the throw-away mutant copy; the benchmark pin stays enforced
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900, env={**os.environ, "WR_FROZEN_SCOPE": "benchmark"})

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out"); a = ap.parse_args()
    out = []
    for mid, desc, rel, old, new in M:
        tmp = tempfile.mkdtemp(prefix="mut_")
        try:
            shutil.copytree(ROOT, os.path.join(tmp, "r"), ignore=shutil.ignore_patterns(".git", "__pycache__", "reports"))
            w = os.path.join(tmp, "r"); path = os.path.join(w, rel); src = open(path).read()
            if src.count(old) != 1:
                out.append({"id": mid, "description": desc, "result": "NOT_APPLIED", "why": f"anchor occurs {src.count(old)}x"}); continue
            open(path, "w").write(src.replace(old, new))
            t = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], w)
            e = run([sys.executable, "-m", "eval.run_eval", "--impl", "final"], w)
            wrong = [l for l in e.stdout.splitlines() if l.strip().startswith("wrong:")]
            killed_by = [n for n, bad in (("unit_tests", t.returncode != 0), ("frozen_benchmark", e.returncode != 0 or any("[]" not in l for l in wrong))) if bad]
            out.append({"id": mid, "description": desc, "file": rel, "result": "KILLED" if killed_by else "SURVIVED", "killed_by": killed_by})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    applied = [m for m in out if m["result"] != "NOT_APPLIED"]
    summ = {"deliberate_fault_injection": True, "mutants": len(out), "applied": len(applied),
            "killed": sum(m["result"] == "KILLED" for m in applied), "survived": [m["id"] for m in applied if m["result"] == "SURVIVED"],
            "not_applied": [m["id"] for m in out if m["result"] == "NOT_APPLIED"], "detail": out}
    print(json.dumps({k: v for k, v in summ.items() if k != "detail"}))
    for m in out: print(f"  {m['id']} {m['result']:11} {m.get('killed_by','')}  {m['description']}")
    if a.out: json.dump(summ, open(a.out, "w"), indent=1)

if __name__ == "__main__":
    main()
