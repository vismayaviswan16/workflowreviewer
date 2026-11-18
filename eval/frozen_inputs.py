"""Verify or create the content manifest for the frozen evaluation inputs.

Normal evaluation uses --check and never regenerates fixtures or labels.
Use --write only when intentionally creating a new benchmark version.
"""
import argparse
import os
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval" / "frozen_manifest.json"

# Inputs, expected labels, split, and the sources/configuration that generated them.
TRACKED = [
    "fixtures/bundles",
    "fixtures/model_scripts",
    "fixtures/split.json",
    "labels/labels.json",
    "fixtures/build_fixtures.py",
    "oracle/cases.py",
    "oracle/make_labels.py",
    "baseline_v0",
]
# Implementation checkpoint (added when the repo was put under git; see README "Freeze and provenance"): the FINAL reviewer and prompt
# are pinned too, so a changed implementation cannot be evaluated against the frozen benchmark without an explicit re-freeze.
IMPL_TRACKED = ["reviewer", "prompts"]

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def collect():
    files = []
    import os
    scope = TRACKED if os.environ.get("WR_FROZEN_SCOPE") == "benchmark" else TRACKED + IMPL_TRACKED   # "benchmark" is used ONLY by eval/mutation_test.py
    for rel in scope:
        p = ROOT / rel
        if not p.exists():
            raise SystemExit(f"Frozen input missing: {rel}")
        paths = sorted(p.rglob("*")) if p.is_dir() else [p]
        for item in paths:
            if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                files.append(item)
    return {
        "format_version": 1,
        "description": "Frozen synthetic evaluation inputs, expected labels, split, generator provenance, baseline checkpoint, and the final implementation checkpoint (reviewer/, prompts/).",
        "files": {
            p.relative_to(ROOT).as_posix(): {
                "sha256": sha256(p),
                "bytes": p.stat().st_size
            }
            for p in sorted(set(files))
        }
    }

def verify():
    if not MANIFEST.exists():
        raise SystemExit("Frozen manifest is missing. Run ./prepare_frozen_eval.sh once to create it.")
    current = collect()
    expected = json.loads(MANIFEST.read_text())
    old, new = expected.get("files", {}), current["files"]
    if os.environ.get("WR_FROZEN_SCOPE") == "benchmark":
        old = {k: v for k, v in old.items() if not k.startswith(tuple(p + "/" for p in IMPL_TRACKED))}
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    if changed:
        print("FROZEN INPUT CHECK FAILED; benchmark inputs or provenance changed:")
        for name in changed:
            print(f"  - {name}")
        raise SystemExit(1)
    print(f"Frozen input check passed ({len(new)} files; labels and benchmark inputs unchanged).")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write/update the manifest intentionally")
    ap.add_argument("--check", action="store_true", help="verify all frozen inputs against the manifest")
    args = ap.parse_args()
    if args.write == args.check:
        ap.error("choose exactly one of --write or --check")
    current = collect()
    if args.write:
        MANIFEST.write_text(json.dumps(current, indent=2) + "\n")
        print(f"Wrote {MANIFEST.relative_to(ROOT)} ({len(current['files'])} files)")
        return
    if not MANIFEST.exists():
        raise SystemExit("Frozen manifest is missing. Run ./prepare_frozen_eval.sh once to create it.")
    verify()

if __name__ == "__main__":
    main()
