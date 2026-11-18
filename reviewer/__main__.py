"""CLI: python -m reviewer BUNDLE.json [--grid-cap N] [--fake-script SCRIPT.json] [--require-model]
Prints the JSON report. Exit code: 0 PASS, 1 CHANGES_REQUIRED, 2 INCOMPLETE, 3 unreadable/invalid input file. Offline; no model unless a fake script is given."""
import argparse, json, sys
from .review import Config, review
from .model.fake import FakeClient

def main(argv=None):
    ap = argparse.ArgumentParser(prog="reviewer")
    ap.add_argument("bundle"); ap.add_argument("--grid-cap", type=int); ap.add_argument("--fake-script"); ap.add_argument("--require-model", action="store_true")
    ap.add_argument("--live-model", help="OPT-IN live model id (needs ANTHROPIC_API_KEY); never used by tests/benchmarks")
    a = ap.parse_args(argv)
    cfg = Config()
    if a.grid_cap: cfg.grid_cap = a.grid_cap
    cfg.require_model = a.require_model
    try:
        client = None
        if a.fake_script:
            with open(a.fake_script, encoding="utf-8") as fh:
                client = FakeClient(json.load(fh))
        if a.live_model:
            from .model.anthropic_client import AnthropicClient
            client = AnthropicClient(a.live_model)
        with open(a.bundle, encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, ValueError) as e:       # unreadable / invalid JSON: exit 3 (never collides with 0/1/2)
        print(json.dumps({"status": "ERROR", "error": f"{type(e).__name__}: {e}"}))
        return 3
    rep = review(bundle, cfg, client)
    print(json.dumps(rep, indent=1, default=str))
    return {"PASS": 0, "CHANGES_REQUIRED": 1, "INCOMPLETE": 2}[rep["status"]]

if __name__ == "__main__":
    sys.exit(main())
