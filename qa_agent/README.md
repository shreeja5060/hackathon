# Q&A agent

**Owner:** Anu

Answers questions about the policies, NIST SP 800-53 Rev. 5 / CSF 2.0 and the
synthetic environment evidence, using Claude with Phase 1's `search()` as a tool.

- **Grounded:** Claude must cite the passages it retrieved, inline as `[1]`, `[2]`.
  If the documents don't answer the question, it says so instead of guessing.
- **Citation check:** every cited reference is checked against the passages
  actually retrieved for that answer; anything else is removed before the
  answer is returned.
- **Logged:** each question, the passages retrieved, the passages cited and the
  answer go to `data/processed/chat_log.sqlite3` (gitignored).

## Use it from the dashboard

The dashboard's live mode loads the agent from an environment variable:

```bash
COPILOT_BACKEND=live
COPILOT_QA_MODULE=qa_agent.agent:ask
```

`ask(question)` returns the dashboard's contract:
`{"answer": str, "citations": [search() results]}`. Citations are in the same
order as the `[1]`, `[2]` markers in the answer.

## Richer entry point

`answer(question, finding=None, history=None)` adds:

- **history:** earlier turns as `{"role": "user"|"assistant", "content": str}`
- **finding:** a finding dict from the gap report. Its cited policy text and its
  NIST control text are preloaded, and Claude can call `propose_recommendation`.
  The result then includes `proposed_recommendation`, a suggestion only: the
  dashboard should change the finding only if the reviewer accepts it.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | From `.env` locally, Secret Manager on Cloud Run |
| `CHAT_MODEL` | `claude-haiku-4-5` | Use `claude-sonnet-5-5` for the demo |

## Search helper

`retrieval.search()` wraps Phase 1's `search()`: a bare control ID (`AC-2`,
`PR.AA-06`) is looked up exactly, since searching an ID by meaning returns a
neighboring control; `type="policy"` is accepted for `"internal"`; and calls
are serialized because Chroma's client isn't thread-safe.
