"""Checkpoint v0: the naive first version (rule-ID matching + numeric substring checks).

Deliberately weak: trusts IDs/source_ref, compares only the numbers found in text, ignores approvals,
operators, order, units, workflow and code semantics, never abstains. Kept unchanged as the comparison point.
NOTE: this file was written as an explicit baseline for comparison; it is not a record of an earlier tuning history.
"""
import re


def _nums(s):
    return set(re.findall(r"\d+", s or ""))


def review_v0(bundle):
    findings = []
    try:
        reqs = [r for r in bundle["requirements"]["items"] if r["text"].upper().startswith("IF")]
        spec = (bundle.get("spec") or {}).get("rules", [])
        refs = {r.get("source_ref"): r for r in spec}
        code_nums = set()
        for s in bundle.get("snippets", []):
            code_nums |= _nums(s.get("source", ""))
        for r in reqs:
            n = _nums(r["text"])
            if r["id"] not in refs:
                findings.append(dict(severity="blocker", category="omitted_requirement", artifact="spec", rule_ref=r["id"],
                                     location=r["anchor"], evidence=[r["text"]], evidence_verified=True))
                continue
            if n and not n <= _nums(refs[r["id"]]["when"]):
                findings.append(dict(severity="blocker", category="changed_threshold", artifact="spec", rule_ref=r["id"],
                                     location="rules", evidence=[r["text"]], evidence_verified=True))
            if n and code_nums and not n <= code_nums:
                findings.append(dict(severity="blocker", category="changed_threshold", artifact="code", rule_ref=r["id"],
                                     location="line", evidence=[r["text"]], evidence_verified=True))
    except Exception as e:  # malformed input: v0 just crashes into a block
        findings.append(dict(severity="blocker", category="error", artifact="bundle", rule_ref=None, location=str(e), evidence=[], evidence_verified=True))
    return {"bundle_id": bundle.get("bundle_id"), "status": "CHANGES_REQUIRED" if findings else "PASS",
            "findings": findings, "model": None}
