# Material AI assistance and how it was verified

## My contribution

I chose the overall architecture and design direction for the reviewer, including the shared intermediate representation (IR), the deterministic semantic-review approach, and the advisory-only role of the model layer. I repeatedly ran the tests and evaluation commands, inspected failures and incorrect outputs, identified issues that needed attention, and used the results to guide and verify fixes. I also personally checked the frozen-input validation, the final benchmark results, CLI exit codes, and a locally modified fixture whose output I inspected for its evidence and counterexample.

## AI assistance

I used Anthropic Claude / Claude Code as a substantial coding and writing assistant. AI drafted the README; generated the fixture generator; and wrote or helped write much of the implementation, and evaluation utilities, including the fuzzer, adversarial probe and mutation-testing harness.

The architecture and design direction were my choices; AI assistance with implementation and documentation should not be mistaken for independent authorship of all the code or tests.

## How I checked the work

1. **Repeated execution and debugging.** I ran the test suite and evaluation commands repeatedly, examined failures and unexpected outputs, identified issues to address, and reran the relevant checks after fixes. The final recorded unit-test run contains 57 passing tests.
2. **Frozen inputs and final evaluation.** I ran the frozen-input check and the final 27-case evaluation. The recorded results are 7/7 valid cases, 14/14 defective cases blocked, and 6/6 incomplete cases classified correctly, with zero false passes and zero false blocks.
3. **CLI behavior.** I checked representative valid, defective, incomplete and unreadable-input cases and inspected their exit codes: 0, 1, 2 and 3 respectively.
4. **Counterexamples.** I changed the threshold in a local copy of a fixture and inspected the resulting finding, evidence and counterexample. The reported counterexamples are also re-evaluated through the two routing implementations before being counted.
5. **Separate oracle and robustness checks.** The repository includes a plain-Python routing oracle separate from the reviewer implementation, plus differential fuzzing, adversarial probes, a second policy family, scaling measurements and mutation testing. These checks provide additional evidence, but AI assistance was used to create parts of the supporting code and tests; they are not represented as wholly unaided work.

## Limitations and claims

The benchmark is finite and the reviewer only claims verification within its declared supported language and resource limits. Unsupported or ambiguous cases should remain INCOMPLETE. 
