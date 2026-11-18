#!/usr/bin/env bash
# INTENTIONAL benchmark regeneration. Review changes before committing the new frozen manifest.
set -euo pipefail
cd "$(dirname "$0")"
python3 fixtures/build_fixtures.py
python3 oracle/make_labels.py
python3 -m eval.frozen_inputs --write
echo "Benchmark inputs regenerated and frozen. Review fixtures/, labels/, and eval/frozen_manifest.json."
