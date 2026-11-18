#!/usr/bin/env bash
# Reproducible CPU-only offline evaluation (Python 3.12+, standard library only). Never regenerates benchmark inputs or labels.
# QUICK=1 ./run_all.sh  skips the slow extras (scaling curve + mutation test); everything else is identical.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
mkdir -p reports
$PY -m eval.frozen_inputs --check                                   # benchmark + baseline + FINAL implementation pinned by SHA-256
$PY -m unittest discover -s tests                                   # unit + regression tests (all offline, fake / stub clients only)
$PY -m eval.run_eval --impl v0    --out reports/v0_all.json         # initial checkpoint, same frozen set
$PY -m eval.run_eval --impl v0    --split holdout --out reports/v0_holdout.json
$PY -m eval.run_eval --impl final --out reports/final_all.json
$PY -m eval.run_eval --impl final --split holdout --out reports/final_holdout.json
for ab in random_sampling no_approvals no_pruning; do
  $PY -m eval.run_eval --impl final --ablation "$ab" --out "reports/ablation_${ab}.json"
done
# --- post-freeze evidence (NOT part of the frozen benchmark): independent fuzzer, adversarial probe, second policy family
$PY -m eval.fuzz_differential --n 400 --seed 1 | tee reports/fuzz_seed1.txt
$PY -m eval.fuzz_differential --n 400 --seed 2 | tee reports/fuzz_seed2.txt
$PY -m eval.adversarial_probe > reports/adversarial_v1.2.0.txt 2>&1
if grep -q MISMATCH reports/adversarial_v1.2.0.txt; then echo "adversarial probe: MISMATCH rows present"; grep MISMATCH reports/adversarial_v1.2.0.txt; exit 1; fi
echo "adversarial probe: 0 mismatches ($(grep -c -- '->' reports/adversarial_v1.2.0.txt) probes)"
$PY -m eval.second_family --out reports/second_family.json
if [ -z "${QUICK:-}" ]; then
  $PY -m eval.scaling_probe --max-k 8 --out reports/scaling.json
  $PY -m eval.mutation_test --out reports/mutation_test.json
fi
echo "ALL CHECKS COMPLETE"
