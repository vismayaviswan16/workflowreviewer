"""Write labels/labels.json. Expected labels are fixed here BEFORE any evaluation.

For valid/defective cases the expected status is *derived* from the independent oracle by brute-force
sweep (does any ticket route differently between the policy and a defective artifact?) and must agree
with the hand-written intent below, otherwise this script aborts. INCOMPLETE cases are labelled by
design (the missing/ambiguous information is the point).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cases import CASES, SWEEP_TICKETS  # noqa: E402

ROOT = os.path.dirname(HERE)


def F(cats, artifact, rule_ref, loc):
    return {"categories": cats, "artifact": artifact, "rule_ref": rule_ref, "location_contains": loc}


META = {
    # id: (type, intended_status, findings, notes)
    "V01": ("valid", "PASS", [], "baseline, all artifacts agree"),
    "V02": ("valid", "PASS", [], "approved 30->25 applied everywhere; proposed (A2) and out-of-scope (A0) records ignored"),
    "V03": ("valid", "PASS", [], "unusual but equivalent formulation: NOT/OR, seconds, reordered exclusions, chained workflow"),
    "V04": ("valid", "PASS", [], "unapproved suggestion + prose approval claim must not change the threshold"),
    "V05": ("valid", "PASS", [], "rename/reorder/paraphrase variant of V01 (pair with D01)"),
    "V06": ("valid", "PASS", [], "rename/reorder variant of V03"),
    "V07": ("valid", "PASS", [], "model returns truncated JSON; model is advisory so deterministic PASS stands, failure disclosed"),
    "D01": ("defective", "CHANGES_REQUIRED", [F(["changed_threshold"], "spec", "R3", "rules[2]")], "spec threshold 35"),
    "D02": ("defective", "CHANGES_REQUIRED", [F(["boundary_mismatch"], "spec", "R3", "rules[2]")], "waiting > 30"),
    "D03": ("defective", "CHANGES_REQUIRED", [F(["precedence_error"], "spec", "R1", "rules[0]")], "R2 before R1"),
    "D04": ("defective", "CHANGES_REQUIRED", [F(["omitted_requirement"], "spec", "R1", "")], "spec omits missing-category rule"),
    "D05": ("defective", "CHANGES_REQUIRED", [F(["changed_threshold"], "spec", "R3", "rules[2]")], "approved 25 not propagated to spec"),
    "D06": ("defective", "CHANGES_REQUIRED", [F(["wrong_destination"], "workflow", "R3", "n1")], "escalation branch -> normal queue"),
    "D07": ("defective", "CHANGES_REQUIRED", [F(["boundary_mismatch"], "code", "R3", "line")], "code uses > despite correct metadata"),
    "D08": ("defective", "CHANGES_REQUIRED", [F(["precedence_error"], "spec", "R1", "rules[0]")], "vip exception before missing-category rule (each rule plausible alone)"),
    "D09": ("defective", "CHANGES_REQUIRED", [F(["missing_branch"], "workflow", "RD", "n1")], "no else branch"),
    "D10": ("defective", "CHANGES_REQUIRED", [F(["unit_mismatch"], "spec", "R3", "rules[2]")], "unitless 30 declared as seconds"),
    "D11": ("defective", "CHANGES_REQUIRED", [F(["changed_threshold"], "spec", "R3", "rules[2]")], "injected instructions + threshold 35"),
    "D12": ("defective", "CHANGES_REQUIRED", [F(["changed_threshold"], "spec", "REQ-C", "rules[2]")], "rename/paraphrase variant of D01; defect must persist"),
    "D13": ("defective", "CHANGES_REQUIRED", [F(["runtime_error"], "code", "RD", "line")], "unguarded comparison crashes when waiting missing"),
    "D14": ("defective", "CHANGES_REQUIRED", [F(["changed_threshold"], "spec", "R3", "rules[2]")], "threshold 35 + model invents refs, conflicts, says PASS"),
    "I01": ("incomplete", "INCOMPLETE", [], "two approved, unordered records for R3 (25 vs 20)"),
    "I02": ("incomplete", "INCOMPLETE", [], "requirement outside supported language must stay visible"),
    "I03": ("incomplete", "INCOMPLETE", [], "snippet outside restricted Python subset"),
    "I04": ("incomplete", "INCOMPLETE", [], "model required; every call times out"),
    "I05": ("incomplete", "INCOMPLETE", [], "grid cap forces sampled equivalence; sampled cannot pass"),
    "I06": ("incomplete", "INCOMPLETE", [], "workflow version differs from the version under review"),
}
HOLDOUT = ["V03", "D07", "D08", "D12", "I01"]
PAIRS = {"V01": ["D01", "V05", "D12"], "V03": ["V06"]}


def sweep_has_diff(cid):
    c = CASES[cid]
    for t in SWEEP_TICKETS:
        e = c["expected"](t)
        for fn in c["actual"].values():
            if fn(t) != e:
                return True
    return False


def main():
    labels = {}
    for cid, (typ, status, findings, notes) in META.items():
        if typ in ("valid", "defective"):
            derived = "CHANGES_REQUIRED" if sweep_has_diff(cid) else "PASS"
            if derived != status:
                sys.exit(f"{cid}: oracle sweep says {derived}, intent says {status}")
        labels[cid] = {"type": typ, "expected_status": status, "expected_findings": findings,
                       "holdout": cid in HOLDOUT, "notes": notes, "oracle": cid in CASES}
    out = {"labels": labels, "pairs": PAIRS, "holdout": HOLDOUT}
    os.makedirs(os.path.join(ROOT, "labels"), exist_ok=True)
    with open(os.path.join(ROOT, "labels", "labels.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("labels:", len(labels), "oracle-checked:", sum(1 for v in labels.values() if v["oracle"]))


if __name__ == "__main__":
    main()
