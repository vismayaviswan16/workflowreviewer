# Material AI assistance and how it was verified

## My contribution

I chose the overall architecture and design direction for the reviewer, including the shared intermediate representation (IR), the deterministic semantic-review approach, and the advisory-only role of the model layer. I repeatedly ran the tests and evaluation commands, inspected failures and incorrect outputs, identified issues that needed attention, and used the results to guide and verify fixes. I also personally checked the frozen-input validation, the final benchmark results, CLI exit codes, and a locally modified fixture whose output I inspected for its evidence and counterexample.

## AI assistance

I used Anthropic Claude / Claude Code as a substantial coding and writing assistant. AI drafted the README, debugging write-ups and this note; generated the fixture generator; and wrote or helped write much of the implementation, tests, and evaluation utilities, including the fuzzer, adversarial probe and mutation-testing harness.

AI also assisted with implementation of the additional work documented in the README, including revocation-event handling, the wider supported Python subset, finding consolidation, the live-client adapter, mutation testing, the second policy family, scaling probe, and Git / manifest-freeze tooling. The live Anthropic client is included but was not run, so no real-model performance is claimed.

The architecture and design direction were my choices; AI assistance with implementation and documentation should not be mistaken for independent authorship of all the code or tests. In particular, AI-generated tests and reports are supporting artifacts, not proof by themselves that the system is correct.

## How I checked the work

1. **Repeated execution and debugging.** I ran the test suite and evaluation commands repeatedly, examined failures and unexpected outputs, identified issues to address, and reran the relevant checks after fixes. The final recorded unit-test run contains 57 passing tests.
2. **Frozen inputs and final evaluation.** I ran the frozen-input check and the final 27-case evaluation. The recorded results are 7/7 valid cases, 14/14 defective cases blocked, and 6/6 incomplete cases classified correctly, with zero false passes and zero false blocks.
3. **CLI behavior.** I checked representative valid, defective, incomplete and unreadable-input cases and inspected their exit codes: 0, 1, 2 and 3 respectively.
4. **Counterexamples.** I changed the threshold in a local copy of a fixture and inspected the resulting finding, evidence and counterexample. The reported counterexamples are also re-evaluated through the two routing implementations before being counted.
5. **Separate oracle and robustness checks.** The repository includes a plain-Python routing oracle separate from the reviewer implementation, plus differential fuzzing, adversarial probes, a second policy family, scaling measurements and mutation testing. These checks provide additional evidence, but AI assistance was used to create parts of the supporting code and tests; they are not represented as wholly unaided work.

## Limitations and claims

The benchmark is finite and the reviewer only claims verification within its declared supported language and resource limits. Unsupported or ambiguous cases should remain INCOMPLETE. The live model adapter was not run, and real-model behavior was not measured. The five holdout bundles are treated as regression evidence rather than certified unseen data because the pre-Git development history cannot establish that they were never inspected during development.
