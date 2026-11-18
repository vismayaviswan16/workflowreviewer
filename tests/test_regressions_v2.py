"""Regression tests for defects found by adversarial testing AFTER the frozen evaluation (post-freeze fixes).
Each test reproduces the original failing behaviour (see reports/debugging/adversarial_before.txt)."""
import copy, json, os, sys, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reviewer.review import review, Config
from reviewer.model.fake import FakeClient

with open(os.path.join(ROOT, "fixtures", "bundles", "V01.json"), encoding="utf-8") as _fh:
    BASE = json.load(_fh)
U = 'UNITS = {"waiting": "min"}\n\n'
H = 'def route(ticket):\n    if ticket.get("category") is None:\n        return "manual_review"\n    if ticket.get("urgent"):\n        return "priority_queue"\n'
T = '    return "normal_queue"\n'


def mut(f):
    b = copy.deepcopy(BASE); f(b); return b


def appr(*recs):
    def f(b): b["approvals"] = list(recs)
    return mut(f)


def A(i, seq, when=None, **kw):
    return {"id": i, "target": kw.pop("target", "R3"), "status": kw.pop("status", "approved"), "seq": seq, "applies_to": "*",
            **({"new_when": when} if when else {"new_then": "escalation"}), **kw}


class BundleCannotRelaxOperatorConfig(unittest.TestCase):      # bug: bundle review_config overrode require_model
    def test_require_model_not_relaxed(self):
        b = mut(lambda b: b.__setitem__("review_config", {"require_model": False}))
        cfg = Config(); cfg.require_model = True
        r = review(b, cfg, None)
        self.assertEqual(r["status"], "INCOMPLETE")
        self.assertTrue(any("cannot relax" in n for n in r["coverage"]["config_notes"]))

    def test_bundle_may_tighten(self):
        b = mut(lambda b: b.__setitem__("review_config", {"require_model": True}))
        self.assertEqual(review(b, Config(), None)["status"], "INCOMPLETE")

    def test_bad_grid_cap_does_not_crash_or_raise_cap(self):       # bug: TypeError on grid_cap="abc"
        for v in ("abc", -5, True, 1.5):
            r = review(mut(lambda b: b.__setitem__("review_config", {"grid_cap": v})), Config(), None)
            self.assertEqual(r["status"], "PASS", v)
        r = review(mut(lambda b: b.__setitem__("review_config", {"grid_cap": 10 ** 12})), Config(grid_cap=50), None)
        self.assertEqual(r["provenance"]["config"]["grid_cap"], 50)


class ContradictoryApprovalsNeverSilentlyResolved(unittest.TestCase):
    def test_supersession_cycle(self):                              # bug: both retired -> base threshold -> PASS
        # same-seq mutual supersession: only the cycle detector can catch it
        r = review(appr(A("A1", 1, "waiting >= 25 min", supersedes=["A2"]), A("A2", 1, "waiting >= 20 min", supersedes=["A1"])))
        self.assertEqual(r["status"], "INCOMPLETE"); self.assertTrue(any("cycle" in x for x in r["incomplete_reasons"]))

    def test_supersession_cycle_with_unordered_seq(self):           # original repro (seq 1 vs 2)
        r = review(appr(A("A1", 1, "waiting >= 25 min", supersedes=["A2"]), A("A2", 2, "waiting >= 20 min", supersedes=["A1"])))
        self.assertEqual(r["status"], "INCOMPLETE")

    def test_three_record_cycle(self):
        r = review(appr(A("A1", 1, "waiting >= 25 min", supersedes=["A3"]), A("A2", 1, "waiting >= 20 min", supersedes=["A1"]),
                        A("A3", 1, "waiting >= 22 min", supersedes=["A2"])))
        self.assertEqual(r["status"], "INCOMPLETE"); self.assertTrue(any("cycle" in x for x in r["incomplete_reasons"]))

    def test_cross_target_supersession(self):                       # bug: A1 silently retired
        r = review(appr(A("A1", 1, "waiting >= 25 min"), A("A2", 2, "urgent == true", target="R2", supersedes=["A1"])))
        self.assertEqual(r["status"], "INCOMPLETE"); self.assertTrue(any("cross-target" in x for x in r["incomplete_reasons"]))

    def test_unknown_supersedes_id(self):                           # bug: ignored silently
        r = review(appr(A("A1", 1, "waiting >= 30 min", supersedes=["ZZ"])))
        self.assertEqual(r["status"], "INCOMPLETE")

    def test_supersedes_later_seq(self):
        r = review(appr(A("A1", 1, "waiting >= 25 min"), A("A2", 5, "waiting >= 30 min", supersedes=["A1"]),
                        A("A3", 3, "waiting >= 20 min", supersedes=["A2"])))
        self.assertEqual(r["status"], "INCOMPLETE")

    def test_valid_chain_still_works(self):                         # no regression on the legitimate path
        b = appr(A("A1", 1, "waiting >= 20 min"), A("A2", 2, "waiting >= 25 min", supersedes=["A1"]))
        r = review(b)
        self.assertEqual(r["status"], "CHANGES_REQUIRED")           # spec/workflow/code still say 30 vs effective 25
        self.assertTrue(all(f["category"] == "changed_threshold" for f in r["findings"] if f["severity"] == "blocker"))


class TimeBudget(unittest.TestCase):
    @staticmethod
    def blowup(n):
        def f(b):
            for i in range(n): b["fields"][f"f{i}"] = {"type": "number"}
            cond = "waiting >= 30 min OR " + " OR ".join(f"f{i} > {i} AND f{(i + 1) % n} > {i + 2}" for i in range(n))
            b["spec"]["rules"][2]["when"] = cond
            b["workflow"]["nodes"][0]["branches"][2]["when"] = cond
            b["requirements"]["items"][2]["text"] = f"IF {cond} THEN escalation"
            b["snippets"] = []
        return mut(f)

    def test_moderate_grid_is_fast_after_fix(self):                 # was 3.7 s
        t = time.time(); r = review(self.blowup(4))
        self.assertEqual(r["status"], "PASS"); self.assertLess(time.time() - t, 2.0)

    def test_blowup_hits_budget_and_is_incomplete(self):            # was >20 s with no bound
        t = time.time(); r = review(self.blowup(8), Config(time_budget_s=2.0))
        self.assertLess(time.time() - t, 8.0)
        self.assertEqual(r["status"], "INCOMPLETE")
        self.assertTrue(any(c.get("mode") == "sampled" for c in r["checks"] if "artifact" in c))


class ModelSeesUnparsedContent(unittest.TestCase):                  # bug: model never saw what the checker could not read
    def run_capture(self, bundle):
        cap = {}
        class C(FakeClient):
            def complete(self, req): cap["p"] = req.prompt; return super().complete(req)
        r = review(bundle, None, C([{"kind": "valid", "body": {"findings": [], "overall_opinion": "INCOMPLETE"}}]))
        return r, cap.get("p", "")

    def test_unsupported_requirement_in_prompt_as_untrusted(self):
        from eval.run_eval import load_bundles
        r, p = self.run_capture(load_bundles()["I02"])
        self.assertIn("Enterprise customers should usually be handled promptly", p); self.assertIn("UNPARSED/UNTRUSTED", p)
        self.assertEqual(r["status"], "INCOMPLETE")

    def test_unliftable_code_in_prompt(self):
        from eval.run_eval import load_bundles
        r, p = self.run_capture(load_bundles()["I03"])
        self.assertIn("int(", p); self.assertEqual(r["status"], "INCOMPLETE")

    def test_model_concern_on_unparsed_cannot_change_status(self):
        from eval.run_eval import load_bundles
        b = load_bundles()["V01"]
        r = review(b, None, FakeClient([{"kind": "valid", "body": {"overall_opinion": "CHANGES_REQUIRED", "findings": [
            {"claim_type": "defect", "rule_ref": "R3", "evidence_ids": [], "summary": "R3 wrong", "correction": "x"}]}}]))
        self.assertEqual(r["status"], "PASS")


class PythonSubset(unittest.TestCase):
    def snip(self, src):
        return review(mut(lambda b: b["snippets"][0].__setitem__("source", U + src)))

    def test_get_default_equivalent(self):
        self.assertEqual(self.snip(H + '    if ticket.get("waiting", 0) >= 30:\n        return "escalation"\n' + T)["status"], "PASS")

    def test_get_default_changes_missing_behaviour(self):
        r = self.snip(H + '    if ticket.get("waiting", 99) >= 30:\n        return "escalation"\n' + T)
        self.assertEqual(r["status"], "CHANGES_REQUIRED")
        self.assertIsNone([f for f in r["findings"] if f["severity"] == "blocker"][0]["counterexample"]["ticket"]["waiting"])

    def test_alias_guarded_ok_unguarded_crash_boundary_caught(self):
        pre = 'def route(ticket):\n    w = ticket.get("waiting")\n    if ticket.get("category") is None:\n        return "manual_review"\n    if ticket.get("urgent"):\n        return "priority_queue"\n'
        self.assertEqual(self.snip(pre + '    if w is not None and w >= 30:\n        return "escalation"\n' + T)["status"], "PASS")
        r = self.snip(pre + '    if w >= 30:\n        return "escalation"\n' + T)
        self.assertEqual([f["category"] for f in r["findings"] if f["severity"] == "blocker"], ["runtime_error"])
        r = self.snip(pre + '    if w is not None and w > 30:\n        return "escalation"\n' + T)
        self.assertEqual([f["category"] for f in r["findings"] if f["severity"] == "blocker"], ["boundary_mismatch"])

    def test_reassignment_stays_visible(self):
        r = self.snip('def route(ticket):\n    w = ticket.get("waiting")\n    w = 5\n    return "normal_queue"\n')
        self.assertEqual(r["status"], "INCOMPLETE")


class SourceRef(unittest.TestCase):
    def test_dangling_source_ref_warns_but_does_not_change_status(self):
        r = review(mut(lambda b: b["spec"]["rules"][2].__setitem__("source_ref", "R9")))
        self.assertEqual(r["status"], "PASS")
        self.assertEqual([f["category"] for f in r["findings"] if f["severity"] == "warning"], ["dangling_source_ref"])


class DifferentialFuzz(unittest.TestCase):
    def test_reviewer_agrees_with_independent_oracle(self):
        from eval.fuzz_differential import run_fuzz
        st, bad = run_fuzz(60, 11)
        self.assertEqual((st["false_pass"], st["false_block"], st["crash"], st["incomplete"]), (0, 0, 0, 0), bad[:3])
        self.assertGreater(st["truly_defective"], 10)


if __name__ == "__main__":
    unittest.main()
