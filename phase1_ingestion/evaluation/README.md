# Retrieval development evaluation

Run from the project root with the existing environment index:

```bash
python -m phase1_ingestion.evaluate_retrieval
```

No index rebuild, new packages or Claude API key is required if the six synthetic
environment chunks are already indexed. `--validate-only` checks case definitions
without loading Chroma or the model.

The 14 cases cover six Computer Security Policy sections, two NIST SP 800-53 MFA
control statements and six synthetic configuration resources. Expected policy
passages were checked against the source PDF, NIST MFA targets against previously
retrieved control statements, and evidence targets against the demo JSON/parser.
The cases are fixed before the initial run. This is a small development set,
not a held-out benchmark or comprehensive coverage of CSF and all policy sources.
Two questions revisit topics already used in smoke tests.

A hit requires the source filename, page and locator to match, and expected text
to be present in the returned passage. Checking the text prevents credit for a
fragment that names a control but omits its relevant statement. Alternative valid
passages can still be marked misses; review those before judging a failure.

- Hit@1: fraction of questions with the expected passage ranked first.
- Hit@3: fraction with the expected passage anywhere in the first three results.
- MRR@3: average reciprocal rank (1, 1/2, 1/3, or 0 for a miss).

The report includes per-category metrics, full results, case-file hash, model
revision and collection name. It is saved to the ignored file
`data/processed/retrieval_evaluation.json`. Missing targets produce a setup error;
retrieval misses are recorded without stopping the remaining questions.

The Jordan case checks retrieval of an unknown value; it does not test whether
an agent interprets that value correctly. All cases measure retrieval, not answer
quality, applicability, operational security or compliance. No score threshold is
used to decide compliance or whether an answer is supported.

Review misses and keep this initial report as a baseline. If tuning later uses
these questions, add separate unseen questions before claiming generalization.
