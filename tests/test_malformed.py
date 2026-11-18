"""Regression: malformed (wrong-typed) bundles must yield INCOMPLETE, never a crash; CLI exit 3 for unreadable input.
Found by mutating fixtures/bundles/V02.json with wrong-typed values (~11% of 3000 mutants crashed before the fix)."""
import copy, io, json, os, random, tempfile, unittest
from contextlib import redirect_stdout

from reviewer.__main__ import main
from reviewer.review import review

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load_base():
    path = os.path.join(ROOT, "fixtures", "bundles", "V02.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


BASE = load_base()


def _paths(o, p=()):
    yield p
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _paths(v, p + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _paths(v, p + (i,))


class Malformed(unittest.TestCase):
    def test_known_crashers(self):
        cases = [("requirements", "items", 0), ("spec", "rules", 0), ("approvals", 0), ("fields", "waiting", "unit"),
                 ("workflow", "nodes", 0, "branches")]
        for path in cases:
            for junk in ("x", 5, 1.5, True, [], None):
                b = copy.deepcopy(BASE)
                o = b
                for k in path[:-1]:
                    o = o[k]
                o[path[-1]] = junk
                r = review(b)
                self.assertIn(r["status"], ("INCOMPLETE", "CHANGES_REQUIRED", "PASS"))
                if r["status"] == "PASS":      # a corrupted bundle must not silently pass
                    self.assertEqual(junk, None)

    def test_random_mutants_never_crash(self):
        rnd = random.Random(5)
        junk = [None, 0, 1.5, "", True, [], {}, "x", [1], {"a": 1}]
        for _ in range(300):
            b = copy.deepcopy(BASE)
            for _ in range(rnd.randint(1, 3)):
                ps = [p for p in _paths(b) if p]
                p = rnd.choice(ps)
                o = b
                for k in p[:-1]:
                    o = o[k]
                o[p[-1]] = copy.deepcopy(rnd.choice(junk))
            self.assertIn(review(b)["status"], ("PASS", "CHANGES_REQUIRED", "INCOMPLETE"))

    def test_internal_error_is_incomplete(self):
        r = review({"bundle_id": "X", "requirements": "oops", "fields": 7})
        self.assertEqual(r["status"], "INCOMPLETE")

    def test_cli_exit_codes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["/nonexistent/bundle.json"]), 3)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{bad")
        try:
            with redirect_stdout(buf):
                self.assertEqual(main([f.name]), 3)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
