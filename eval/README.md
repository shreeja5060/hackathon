# Phase 2 accuracy check (the "answer key")

`gold_set.json` holds 15 real policy requirements taken verbatim from the three
sample PDFs. For each one, a human fills in the correct answer. Then
`phase2_agents/evaluate.py` runs the Mapper and Auditor on the same 15 and
reports how often they matched the human.

## How to fill it in (about 1 hour, no coding)

Open `gold_set.json`. For each item, fill three fields:

- `expected_control` - the single best NIST SP 800-53 Rev 5 control ID for this
  requirement, e.g. `"IA-5"`. `hint_control` is a suggestion to start from;
  confirm it by reading the control's text in the NIST catalog
  (https://csrc.nist.gov/projects/cprt/catalog#/cprt/framework/version/SP_800_53_5_1_1/home
  or `data/frameworks/NIST_SP-800-53_rev5_catalog.json`) before copying it in.
- `acceptable_controls` - any other control IDs you would also accept as a
  correct mapping, e.g. `["IA-5", "IR-6"]`. Include `expected_control` here too.
- `expected_coverage` - one of `"Full"`, `"Partial"`, `"Missing"`. Ask: does this
  policy sentence fully satisfy what that control requires? If it only partly
  does (vague scope, missing frequency, etc.), it is `"Partial"`, and say why
  in `notes`.

Leave `hint_control` as is; it is not used for scoring.

## Running the check (needs the API key)

    python phase2_agents/evaluate.py

It prints, per item, what the pipeline said vs. what you said, then:

- Mapper accuracy: how often the picked control was in `acceptable_controls`
- Auditor agreement: how often the coverage verdict matched `expected_coverage`
- Citation validity: how often the citation resolved to the right chunk

Results are also written to `eval/results.json` so they can go in the README.
