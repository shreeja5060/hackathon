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

`ask(question, findings=None, history=None)` returns the dashboard's contract:
`{"answer": str, "citations": [search() results], "suggestions": [...]}`.
Citations are in the same order as the `[1]`, `[2]` markers in the answer.

Both extras are optional, so `ask(question)` still works:

- **findings:** the current review's findings (the dashboard's `Finding` objects
  or plain gap-report dicts). Claude then sees each finding's ID, status,
  coverage, recommendation and reviewer note, so a reviewer can ask things like
  "suggest better recommendations for the 2 rejected findings". It fetches a
  finding's policy and NIST text with `get_finding_evidence` when it needs them.
- **history:** earlier chat messages as `{"role": "user"|"assistant", "content": str}`.
  An assistant message's `citations` are listed with it, so follow-ups know
  what earlier `[n]` markers referred to. Messages with `"error": True` are skipped.

**suggestions** lists `{"finding_id", "recommendation"}` when Claude suggested new
wording. The same suggestions appear in the answer text. They never change a
finding: a reviewer decides whether to use them.

From the dashboard (`app.py`):

```python
session = st.session_state.review_session
backend.ask(question,
            findings=session.findings if session else None,
            history=st.session_state.chat[:-1])
```

## Finding-focused entry point

`answer(question, finding=None, history=None, findings=None)` also takes one
`finding` to focus on: its policy text and NIST control text are preloaded, and
the result includes `proposed_recommendation` when Claude suggested new wording.

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
