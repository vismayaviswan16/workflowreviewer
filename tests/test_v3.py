"""Regression tests for the v1.2.0 hardening (revocation events, wider Python subset, root-cause grouping,
fuzzer oracle domain, live-client mapping with a stub transport). Offline, standard library only."""
import copy, io, json, os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reviewer.review import review
from reviewer.pysubset import lift_python
from reviewer.schemas import FieldDef
from reviewer.model.anthropic_client import AnthropicClient
from reviewer.model.client import ModelRequest, ModelTimeout
from fractions import Fraction

def bundle(name="V01"):
    path = os.path.join(ROOT, "fixtures", "bundles", f"{name}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def with_approvals(recs):
    b = bundle(); b["approvals"] = recs; return b

A1 = {"id": "A1", "target": "R3", "status": "approved", "seq": 1, "applies_to": "*", "new_when": "waiting >= 25 min"}

class Revocation(unittest.TestCase):
    def test_revocation_restores_source_requirement(self):
        r = review(with_approvals([A1, {"id": "A2", "target": "R3", "status": "revoked", "seq": 2, "applies_to": "*", "supersedes": ["A1"]}]))
        self.assertEqual(r["status"], "PASS")
        self.assertIn({"id": "A1", "reason": "revoked by A2"}, r["coverage"]["approvals"]["ignored"])
    def test_without_revocation_the_approval_binds(self):
        self.assertEqual(review(with_approvals([A1]))["status"], "CHANGES_REQUIRED")
    def test_revocation_naming_nothing_is_ambiguous_not_pass(self):
        r = review(with_approvals([A1, {"id": "A2", "target": "R3", "status": "revoked", "seq": 2, "applies_to": "*"}]))
        self.assertEqual(r["status"], "INCOMPLETE")
    def test_revocation_unknown_cross_target_and_earlier_seq(self):
        for rec in ({"supersedes": ["ZZ"]}, {"supersedes": ["A1"], "target": "R2"}, {"supersedes": ["A1"], "seq": 0}):
            rv = {"id": "A2", "target": "R3", "status": "revoked", "seq": 2, "applies_to": "*", **rec}
            self.assertEqual(review(with_approvals([A1, rv]))["status"], "INCOMPLETE", rec)
    def test_out_of_scope_revocation_does_not_revoke(self):
        rv = {"id": "A2", "target": "R3", "status": "revoked", "seq": 2, "applies_to": ["v9"], "supersedes": ["A1"]}
        self.assertEqual(review(with_approvals([A1, rv]))["status"], "CHANGES_REQUIRED")

F = {"category": FieldDef("category", "categorical", None, False, None, None),
     "urgent": FieldDef("urgent", "bool", None, False, None, None),
     "waiting": FieldDef("waiting", "number", "minutes", False, None, None)}

def routes(src):
    lr = lift_python(src, F); assert lr.complete, lr.unsupported; return lr.dl

class PySubset(unittest.TestCase):
    def test_is_true_in_tuple_and_ternary_lift(self):
        dl = routes('UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("category") is None:\n        return "manual_review"\n'
                    '    if t.get("urgent") is True:\n        return "priority_queue"\n'
                    '    if t.get("category") in ("fraud", "legal"):\n        return "manual_review"\n'
                    '    return "escalation" if t.get("waiting", 0) >= 30 else "normal_queue"\n')
        self.assertEqual(len(dl.rules), 4); self.assertEqual(dl.default, "normal_queue")
    def test_ternary_in_if_body(self):
        dl = routes('UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("urgent") is True:\n        return "priority_queue" if t.get("waiting", 0) > 5 else "normal_queue"\n    return "escalation"\n')
        self.assertEqual([r.route for r in dl.rules], ["priority_queue", "normal_queue"])
    def test_not_in_empty_and_non_literal_remain_unsupported(self):
        for src in ('UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("category") in ():\n        return "a"\n    return "b"\n',
                    'UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("category") in OTHER:\n        return "a"\n    return "b"\n',
                    'UNITS = {"waiting": "min"}\ndef route(t):\n    return "a" if t.get("waiting", 0) > t.get("x") else "b"\n'):
            self.assertFalse(lift_python(src, F).complete, src)
    def test_is_true_on_non_bool_stays_unsupported(self):
        self.assertFalse(lift_python('UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("category") is True:\n        return "a"\n    return "b"\n', F).complete)
    def test_equivalent_idioms_pass_end_to_end(self):
        b = bundle()
        b["snippets"] = [{"id": "c1", "version": b["review_version"], "linked_node": None, "source":
            'UNITS = {"waiting": "min"}\ndef route(t):\n    if t.get("category") is None:\n        return "manual_review"\n    if t.get("urgent") is True:\n        return "priority_queue"\n'
            '    return "escalation" if (t.get("waiting") is not None and t.get("waiting") >= 30) else "normal_queue"\n'}]
        self.assertEqual(review(b)["status"], "PASS")
        b["snippets"][0]["source"] = b["snippets"][0]["source"].replace(">= 30", "> 30")
        self.assertEqual(review(b)["status"], "CHANGES_REQUIRED")

class Grouping(unittest.TestCase):
    def test_one_defective_rule_one_finding_per_artifact(self):
        b = bundle(); b["spec"]["rules"][2]["when"] = "waiting > 30 min"
        r = review(b); bl = [f for f in r["findings"] if f["severity"] == "blocker" and f["artifact"] == "spec"]
        self.assertEqual(r["status"], "CHANGES_REQUIRED"); self.assertEqual(len(bl), 1)
    def test_related_findings_keep_information(self):
        import random
        from eval.fuzz_differential import rnd_cond, ROUTES, bundle as fb
        r = random.Random(3); seen = 0
        for i in range(60):
            req = [(rnd_cond(r), r.choice(ROUTES[:3])) for _ in range(r.choice([2, 3, 4]))]
            sp = copy.deepcopy(req); j = r.randrange(len(sp)); sp[j] = (rnd_cond(r), sp[j][1])
            rep = review(fb(req, sp, i))
            for f in rep["findings"]:
                for rel in f.get("related_findings", []):
                    seen += 1; self.assertIsNotNone(rel["ticket"]); self.assertEqual(f["root_cause_group_size"], 1 + len(f["related_findings"]))
        self.assertGreater(seen, 0)

class LiveClientMapping(unittest.TestCase):
    class _H:
        def __init__(s, d): s.d = d
        def read(s): return json.dumps(s.d).encode()
        def __enter__(s): return s
        def __exit__(s, *a): return False
    def req(self): return ModelRequest("p", "m", 0.0, 100, 0, 5.0, "req-1")
    def test_maps_text_usage_and_truncation(self):
        seen = {}
        def op(r, timeout): seen["body"] = json.loads(r.data); seen["t"] = timeout; return self._H({"model": "m", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "{\"a\":"}], "usage": {"input_tokens": 3, "output_tokens": 4}})
        resp = AnthropicClient("m", api_key="k", opener=op).complete(self.req())
        self.assertEqual((resp.finish_reason, resp.usage["completion_tokens"], seen["t"], seen["body"]["temperature"]), ("length", 4, 5.0, 0.0))
    def test_timeout_and_transport_error_are_failures_not_pass(self):
        import socket
        def op(r, timeout): raise socket.timeout()
        with self.assertRaises(ModelTimeout): AnthropicClient("m", api_key="k", opener=op).complete(self.req())
        def op2(r, timeout): raise OSError("boom")
        with self.assertRaises(ModelTimeout): AnthropicClient("m", api_key="k", opener=op2).complete(self.req())
        # through the reviewer: required model that always fails can never PASS
        from reviewer.review import Config
        cfg = Config(); cfg.require_model = True
        self.assertEqual(review(bundle(), cfg, AnthropicClient("m", api_key="k", opener=op2))["status"], "INCOMPLETE")

class FuzzOracleDomain(unittest.TestCase):
    def test_oracle_domain_contains_unmentioned_categorical_value(self):
        from eval.fuzz_differential import TICKETS
        self.assertTrue(any(t["tier"] == "other" for t in TICKETS)); self.assertTrue(any(t["category"] == "other" for t in TICKETS))
    def test_fuzzer_clean(self):
        from eval.fuzz_differential import run_fuzz
        st, bad = run_fuzz(120, 1); self.assertEqual((st["false_pass"], st["false_block"], st["crash"]), (0, 0, 0), bad)

class SurvivorKillers(unittest.TestCase):
    """Added after the mutation test (eval/mutation_test.py) showed these three injected faults survived."""
    def _hdr(self): return 'UNITS = {"waiting": "min"}\ndef route(t):\n'
    def test_none_not_equal_literal_is_true_in_python(self):       # kills M07
        dl = routes(self._hdr() + '    if t.get("category") != "fraud":\n        return "a"\n    return "b"\n')
        from reviewer.ir import DLRouter, OTHER
        rt = DLRouter("c", dl)
        self.assertEqual(rt.route3({"category": None, "urgent": False, "waiting": Fraction(1)}).route, "a")
        self.assertEqual(rt.route3({"category": "fraud", "urgent": False, "waiting": Fraction(1)}).route, "b")
    def test_not_in_is_negated_in_and_missing_not_in_is_true(self):  # kills M13
        from reviewer.ir import DLRouter
        rt = DLRouter("c", routes(self._hdr() + '    if t.get("category") not in ("fraud", "legal"):\n        return "a"\n    return "b"\n'))
        g = lambda c: rt.route3({"category": c, "urgent": False, "waiting": Fraction(1)}).route
        self.assertEqual((g("fraud"), g("legal"), g("billing"), g(None)), ("b", "b", "a", "a"))
    def test_invented_rule_refs_and_evidence_ids_discarded(self):   # kills M09
        from reviewer.model.advisor import check_findings
        data = {"findings": [{"claim_type": "defect", "summary": "x", "rule_ref": "R99", "evidence_ids": []},
                             {"claim_type": "defect", "summary": "y", "rule_ref": "R3", "evidence_ids": ["E404"]},
                             {"claim_type": "concern", "summary": "z", "rule_ref": "R3", "evidence_ids": ["E1"]}]}
        acc, disc, conf = check_findings(data, {"R3"}, {"E1"})
        self.assertEqual([f["summary"] for f in acc], ["z"])
        self.assertEqual(sorted(d["reason"] for d in disc), ["invented_evidence_id", "invented_rule_ref"])
    def test_conflicting_model_claims_discarded(self):
        from reviewer.model.advisor import check_findings
        data = {"findings": [{"claim_type": "ok", "summary": "a", "rule_ref": "R3", "evidence_ids": []},
                             {"claim_type": "defect", "summary": "b", "rule_ref": "R3", "evidence_ids": []}]}
        acc, disc, conf = check_findings(data, {"R3"}, set())
        self.assertEqual((acc, len(conf)), ([], 1))

if __name__ == "__main__":
    unittest.main()
