"""Offline tests (stdlib unittest). Run: python -m unittest discover -s tests -v"""
import json, os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "oracle"))
from eval.run_eval import load_bundles, load_script, finding_matches
from reviewer.review import review, Config
from reviewer.model.fake import FakeClient
from reviewer.equiv import explore, full_cross, build_grid, sample_cells
from reviewer.ir import DLRouter, ERR

B = load_bundles()
with open(os.path.join(ROOT, "labels", "labels.json"), encoding="utf-8") as _fh:
    LAB = json.load(_fh)["labels"]
def run(cid, cfg=None, cache=None):
    s = load_script(cid)
    c = FakeClient(s or [{"kind": "valid", "body": {"findings": [], "overall_opinion": "PASS"}}])
    return review(B[cid], cfg, c, cache)

class Decisions(unittest.TestCase):
    def test_all_labels(self):
        for cid, lab in LAB.items():
            self.assertEqual(run(cid)["status"], lab["expected_status"], cid)

class Regression(unittest.TestCase):
    def test_bug1_default_rule_ref_on_runtime_error(self):          # debugging example 1
        f = [x for x in run("D13")["findings"] if x["severity"] == "blocker"][0]
        self.assertEqual(f["category"], "runtime_error"); self.assertEqual(f["rule_ref"], "RD")
    def test_bug2_one_omission_one_finding(self):                    # debugging example 2
        om = [x for x in run("D04")["findings"] if x["category"] == "omitted_requirement"]
        self.assertEqual(len(om), 1)

class Safety(unittest.TestCase):
    def test_injection_cannot_flip_status(self):
        self.assertEqual(run("D11")["status"], "CHANGES_REQUIRED")
    def test_model_says_pass_cannot_clear_blocker(self):
        self.assertEqual(run("D14")["status"], "CHANGES_REQUIRED")
    def test_model_failure_not_pass_when_required(self):
        self.assertEqual(run("I04")["status"], "INCOMPLETE")
    def test_sampled_cannot_pass(self):
        r = review(B["V01"], Config(grid_cap=3), None)
        self.assertEqual(r["status"], "INCOMPLETE"); self.assertTrue(all(c.get("mode") == "sampled" for c in r["checks"] if "artifact" in c))
    def test_sampled_never_false_pass_and_blocks_when_hit(self):
        for cid in ("D01", "D02", "D03", "D06"):          # defective bundles: sampled run may abstain, must never PASS
            self.assertNotEqual(review(B[cid], Config(grid_cap=3), None)["status"], "PASS", cid)
        self.assertEqual(review(B["D06"], Config(grid_cap=3), None)["status"], "CHANGES_REQUIRED")
    def test_cache_not_stale(self):
        class C(dict):
            def put(self, k, v): self[k] = v
        c = C()
        self.assertEqual(review(B["V01"], None, None, c)["status"], "PASS")
        self.assertEqual(review(B["D01"], None, None, c)["status"], "CHANGES_REQUIRED")
        self.assertTrue(review(B["V01"], None, None, c)["cache"]["hit"])

class Equivalence(unittest.TestCase):
    def test_pruning_is_exact(self):
        from reviewer import effective as eff
        from reviewer.schemas import parse_bundle
        from reviewer.review import _parse_spec
        for cid in ("D01", "D02", "D03", "D04", "V03", "D08"):
            b = parse_bundle(B[cid]); E = eff.derive(b); dl, _ = _parse_spec(b.spec, b.fields)
            routers = {"ref": DLRouter("ref", E.dl()), "spec": DLRouter("spec", dl)}
            g = build_grid(b.fields, [r.cond for r in E.rules] + [r.cond for r in dl.rules])
            pruned = explore(routers, g, 10**7, True); unpruned = explore(routers, g, 10**7, False)
            self.assertEqual(sum(x.covered for x in pruned.regions if x.outs["ref"].route != x.outs["spec"].route),
                             sum(x.covered for x in unpruned.regions if x.outs["ref"].route != x.outs["spec"].route), cid)
            self.assertLessEqual(pruned.leaves, unpruned.leaves)
    def test_sample_determinism(self):
        from reviewer.schemas import parse_bundle
        from reviewer import effective as eff
        b = parse_bundle(B["V01"]); E = eff.derive(b); g = build_grid(b.fields, [r.cond for r in E.rules])
        self.assertEqual(sample_cells(g, 50, 7), sample_cells(g, 50, 7))

class Metamorphic(unittest.TestCase):
    def test_pairs_keep_verdict(self):
        self.assertEqual(run("V01")["status"], run("V05")["status"])
        self.assertEqual(run("D01")["status"], run("D12")["status"])
        self.assertEqual(run("V03")["status"], run("V06")["status"])

if __name__ == "__main__":
    unittest.main()
