# AI-assisted workflow reviewer (v1.2.1)

Reviews whether a **specification, workflow graph and linked Python snippets** implement **written requirements** (+ approved changes).
Deterministic core -- one IR, exact partition-grid equivalence, ast-only Python lifting -- with an **advisory-only** model layer.
Python 3.12+, **standard library only** (developed for 3.12; run here on 3.13), CPU only, offline. The shipped model client is a scripted **FAKE**;
a live Anthropic client exists but was **not run**, so **no real-model performance was measured**.

## One command

    .\run_all.ps1           # Windows PowerShell 5.1; full offline verification
    .\run_all.ps1 -Quick    # skips scaling curve + mutation test
    ./run_all.sh             # macOS / Linux equivalent

Verifies the frozen SHA-256 manifest (benchmark inputs, labels, split, generators, oracle, baseline **and the final `reviewer/`**),
runs 57 unit/regression tests, baseline-vs-final on the same frozen set, the holdout, three ablations, two independent fuzz runs,
the adversarial probe (fails on any MISMATCH), the second-policy-family check, the scaling curve and the mutation test.
It never regenerates fixtures or labels (`./prepare_frozen_eval.sh` does, deliberately, and must be reviewed).

    python -m reviewer fixtures/bundles/D01.json      # one bundle -> JSON report; exit 0 PASS / 1 CHANGES_REQUIRED / 2 INCOMPLETE / 3 unreadable input

## Deliverables map

| Brief asks for | Where |
|---|---|
| Engineering note (<=4 pages) | `docs/ENGINEERING_NOTE.md` (+ `.pdf`) |
| AI-assistance page (<=1 page) | `docs/AI_ASSISTANCE.md` (+ `.pdf`) |
| Schemas / contracts / supported language | `reviewer/schemas.py`, `lang.py`, `pysubset.py`, `effective.py` (docstrings) |
| Synthetic fixtures, labels, seeds, split | `fixtures/`, `labels/labels.json`, `fixtures/split.json`, `oracle/` (independent routing oracle) |
| Baseline vs final, ablations, holdout | `reports/{v0,final}_{all,holdout}.json`, `reports/ablation_*.json` |
| Two debugging examples + regression tests | `reports/debugging/DEBUGGING.md`, `tests/test_regressions_v2.py`, `tests/test_v3.py` |
| Fake vs live model | `reviewer/model/{fake,anthropic_client}.py`, `prompts/reviewer_prompt.md` (live: not run) |
| Generalisation / robustness beyond the benchmark | `reports/second_family.json`, `fuzz_seed*.txt`, `adversarial_v1.2.0.txt`, `scaling.json`, `mutation_test.json` |

## Status rules

Any evidenced blocker -> CHANGES_REQUIRED; else any unsupported / ambiguous / sampled / failed-required check -> INCOMPLETE; else PASS.
The model never affects status. Anything outside the supported language stays visible under `coverage.unreviewed`.

## Freeze and provenance (read this)

* `eval/frozen_manifest.json` SHA-256 pins the synthetic benchmark bundles, labels, split, fixture generator, independent oracle, baseline checkpoint, and final `reviewer/` plus `prompts/`. The benchmark inputs, labels, split and baseline are preserved; the final reviewer checkpoint is separately pinned so accidental implementation drift is detected.
* The reviewer was hardened after the original benchmark freeze (v1.1.0 and v1.2.0 changes below), driven by adversarial probes and fuzzing rather than benchmark labels. The final benchmark reports were recomputed with the final implementation. No fixture labels were changed to improve scores.
* Pre-git history does not exist, so we cannot prove the 5 holdout bundles were never looked at while developing v1.0. They are therefore
  reported as a regression check, and the **independent evidence of generalisation is the second family (19 cases), the fuzzers and the probe**.

## v1.2.1 release hardening

This release hardens malformed-input handling and CLI error codes, closes file handles deterministically in tests and evaluation utilities, adds a native Windows PowerShell runner, and synchronizes submission documentation. It does not change the frozen benchmark inputs or expected labels.

## v1.2.0 changes (found by adversarial testing, not part of the frozen benchmark)

| # | Defect / gap | Fix | Test |
|---|---|---|---|
| 1 | Revocation was a status, so a revoked approval stayed live (false block) | Revocation is an **event** retiring named earlier same-target approvals; bad revocations -> ambiguity | `test_v3.Revocation` |
| 2 | One defective rule produced several blockers (462 artifact-level multi-blocker instances in 600 single-defect fuzz cases; 116 after) | Root-cause consolidation; nothing dropped (`related_findings`) | `test_v3.Grouping` |
| 3 | `x is True`, `in (...)`, `not in`, ternary returns -> INCOMPLETE | Lifted exactly (Python missing-value semantics preserved) | `test_v3.PySubset` |
| 4 | Fuzzer oracle domain missed unmentioned categorical values (3 "false blocks") | Oracle domain fixed | `test_v3.FuzzOracleDomain` |
| 5 | Probe expectation for `30 min` on a seconds-declared spec was wrong | Corrected (explicit unit wins) | probe |
| 6 | Exhaustive check capped at ~7 extra fields | Cap 200k -> 1.5M leaves + wall-clock budget; curve measured | `reports/scaling.json` |
| 7 | No live model adapter | Stdlib Anthropic client, opt-in, stub-tested, **not run** | `test_v3.LiveClientMapping` |
| 8 | Reviewer never mutation-tested | 16 injected faults; 13/16 killed first, 16/16 after 4 new tests | `eval/mutation_test.py` |

| 9 | Wrong-typed bundles crashed the reviewer (~11% of 3,000 mutants of V02); CLI exit 1 on unreadable input collided with CHANGES_REQUIRED | `review()` converts any internal error to INCOMPLETE (`internal_error` field); CLI exits 3 on unreadable/invalid JSON | `tests/test_malformed.py` (fails on the old code, 0 crashes after) |

Known limitations are listed in `docs/ENGINEERING_NOTE.md` section 7.
