# Debugging evidence

Every example below is labelled **REAL** (a defect that occurred, was observed, and was fixed) or **DELIBERATE FAULT INJECTION**.
Nothing here is an invented failure history. The files named `*_before_*` are verbatim program output captured from the v1.1.0
implementation (the checkpoint as received before this round of fixes); `*_after_*` are the same commands on v1.2.0.
Earlier history (before the repo was put under git) is kept only as the files received -- `adversarial_before.txt`,
`adversarial_after.txt`, `bug1_before.txt`, `bug2_before.txt` -- and its provenance cannot be independently verified, so it is
*not* used as one of the two required examples.

## Example 1 -- REAL: revocation was a record status, not an event (reviewer defect)

**Failing output** (`ex1_revocation_before_v1.1.0.txt`, produced by `python -m eval.adversarial_probe` on v1.1.0):

    revocation record (revoke A1)   -> CHANGES_REQUIRED  exp=PASS <<< MISMATCH
    revocation w/o payload          -> INCOMPLETE        exp=PASS <<< MISMATCH

Setup: approval A1 changes R3 to `waiting >= 25 min`; a later record A2 with `status: revoked` and `supersedes: [A1]` withdraws it;
spec/workflow/code all say 30 minutes (the source text). Correct review = PASS.

**Diagnosis.** `effective.derive` ignored every record whose status is not `approved`. A `revoked` record was therefore dropped
and the approval it withdrew (A1) stayed live, so the reviewer applied 25 minutes and flagged the correct 30-minute artifacts as
`changed_threshold` blockers -- a **false block with a wrong explanation**. Separately the schema demanded a payload
(`new_when`/`new_then`) even for a revocation, so a payload-less revocation failed validation and gave INCOMPLETE.
Root cause: the contract modelled approval state, but revocation is a *change over time* (an event with ordering).

**Fix** (focused; `reviewer/effective.py`, `reviewer/schemas.py`): a `revoked` record in scope is a retraction event. It retires the
approvals listed in its `supersedes` if each exists, has the same target and an earlier-or-equal `seq`; otherwise it is an
*ambiguity* (target unresolved -> INCOMPLETE, never a convenient guess). It needs no payload. The contract is documented in the
module docstring.

**Regression tests** (`tests/test_v3.py::Revocation`): revocation restores the source requirement (PASS); without the revocation the
approval still binds (CHANGES_REQUIRED); a revocation naming nothing / an unknown id / another target / a later record is INCOMPLETE; an
out-of-scope revocation does not revoke. Mutant **M11** (revocation ignored) is killed by these tests (`reports/mutation_test.json`).
After: `ex1_revocation_after_v1.2.0.txt` shows both probes OK.

## Example 2 -- REAL: the differential fuzzer reported correct reviewer results as false blocks (harness defect)

**Failing output** (`ex2_fuzzer_before_v1.1.0.txt`): `python -m eval.fuzz_differential --n 400 --seed 1` exited non-zero:

    {"n": 400, "agree": 397, "false_pass": 0, "false_block": 3, ...}   (cases 159, 166, 260)

**Diagnosis.** The three cases were replayed with the fuzzer's own independent evaluator on the reviewer's counterexample ticket
(printed in the same file): in every case the independent evaluator **agrees** that requirements and spec route the ticket
differently, so the reviewer was right and the *harness oracle* was wrong. The counterexamples all use `tier = __other__`
(a value no rule mentions, e.g. the effect of `tier != std`). The fuzzer's brute-force ticket grid only contained the values named
in the rules (`vip`, `std`, missing), so it could never see the difference and labelled the mutant "equivalent". The reviewer
models categorical fields as `named values + one "other" value`, which is the correct reading of an open domain.

**Fix** (`eval/fuzz_differential.py`, harness only; the reviewer was not changed for this example): the oracle grid now includes an
unmentioned value (`"other"`) for each categorical field.

**Regression tests** (`tests/test_v3.py::FuzzOracleDomain`): the grid contains an unmentioned categorical value, and a 120-case fuzz run
must report 0 false passes / 0 false blocks / 0 crashes. After: 800 further cases over two seeds agree 800/800
(`ex2_fuzzer_after_v1.2.0.txt`, `fuzz_seed1.txt`, `fuzz_seed2.txt`).

## Example 3 -- REAL (small): my own test case was wrong, the reviewer was right (second policy family)

First run of `eval/second_family.py`: case N02 (a "valid unusual formulation") was reported CHANGES_REQUIRED. The spec used
`NOT amount <= 200`; with a *missing* amount the inner comparison is false, so the NOT is true and the spec routes the ticket to
`manager_approval` while the requirement does not. The reviewer's counterexample was correct; the case author (me) forgot the
missing-value contract. The case was rewritten to `NOT (amount <= 200 OR amount is missing)` (valid) and the original form was kept
as defective case **N19**. The harness refuses to run when the independent oracle disagrees with a declared label.

## DELIBERATE FAULT INJECTION -- mutation test of the reviewer (`eval/mutation_test.py`)

16 injected faults (approvals honoured when unapproved, scope ignored, conflicts not detected, INCOMPLETE never reported, minutes
as seconds, cache key ignoring the bundle, `not in` lifted as `in`, ...). A mutant is *killed* if the unit tests fail or the frozen
benchmark gets a case wrong. First run: **13/16 killed; M07, M09, M13 survived**. Four tests were then added
(`tests/test_v3.py::SurvivorKillers`); second run: **16/16 killed** (`reports/mutation_test.json`). The tests were strengthened *after*
seeing the survivors; that is the point of the exercise and is disclosed here.
