"""The starter policy writer: grounded in NIST controls, validated AI drafting, checkable by the analyzer."""

import json
import types
from collections import Counter

import pytest

from phase3_dashboard.backends.base import PolicyDocument
from phase3_dashboard.core import analysis
from policy_writer import catalog, writer
from policy_writer.catalog import CONTROLS, TOPICS, Profile, recommended_topics

BAKERY = Profile(org="Maple Street Bakery", size="1-10", sector="Hospitality", it_support="owner",
                 uses=frozenset({"cloud_office", "laptops", "personal_phones", "card_payments", "office_wifi",
                                 "customer_data"}))


def test_every_clause_is_tied_to_a_known_low_or_moderate_control():
    for topic in TOPICS:
        assert topic.clauses, topic.id
        for clause in topic.clauses:
            title, baseline = CONTROLS[clause.control]
            assert title and baseline in {"low", "moderate"}
            assert clause.when in (None, *catalog.USES), clause
    lows = sum(1 for _, baseline in CONTROLS.values() if baseline == "low")
    assert lows / len(CONTROLS) > 0.75  # mostly the low baseline, as the page says


def test_topics_follow_the_answers():
    office_only = Profile(org="Firm", uses=frozenset())
    assert "remote_work" not in recommended_topics(office_only)
    assert "vendors" not in recommended_topics(office_only)
    assert {"remote_work", "vendors"} <= set(recommended_topics(BAKERY))
    assert "vendors" in recommended_topics(Profile(org="Firm", it_support="provider"))


def test_clauses_are_filled_in_for_the_organization():
    draft = writer.draft_policy(BAKERY, recommended_topics(BAKERY))
    text = writer.to_markdown(draft)
    assert "{" not in text and "Maple Street Bakery" in text
    assert "Card payments must be taken only through our payment provider" in text  # card_payments answer
    assert "every 6 months" in text  # a small organization reviews accounts twice a year
    assert "Customer personal data must be encrypted" in text
    no_cards = writer.draft_policy(Profile(org="Firm"), ["vendors", "data_backups"])
    assert "card numbers" not in writer.to_markdown(no_cards)
    assert "our IT provider" in writer.to_markdown(writer.draft_policy(Profile(org="Firm", it_support="provider"),
                                                                       ["accounts"]))


def test_draft_needs_a_name_and_a_topic():
    with pytest.raises(writer.DraftError, match="name"):
        writer.draft_policy(Profile(org="  "), ["accounts"])
    with pytest.raises(writer.DraftError, match="topic"):
        writer.draft_policy(BAKERY, [])


def test_exports_neutralize_the_organizations_own_text():
    sneaky = Profile(org="<script>x</script> [click](https://evil.test) *Bakery*")
    draft = writer.draft_policy(sneaky, ["acceptable_use"])
    html_page = writer.to_html(draft)
    assert "<script>" not in html_page and "&lt;script&gt;" in html_page
    markdown = writer.to_markdown(draft)
    assert "[click](" not in markdown and "\\[click\\]" in markdown and "<script>" not in markdown


def test_the_draft_becomes_phase1_chunks_without_its_control_tags():
    draft = writer.draft_policy(BAKERY, recommended_topics(BAKERY))
    chunks = writer.to_chunks(draft)
    assert set(chunks[0]) == {"chunk_id", "text", "source", "type", "doc_kind", "page", "locator"}
    assert all(c["chunk_id"].startswith("policy-") and c["type"] == "internal" for c in chunks)
    assert [c["locator"] for c in chunks][:4] == ["Section 1 Purpose", "Section 2 Scope", "Section 3 Roles",
                                                  "Section 4.1 Acceptable use"]
    body = " ".join(c["text"] for c in chunks)
    assert not any(control in body for control in ("PL-4", "IA-5(1)", "`"))  # the check judges the words alone


def test_the_analyzer_checks_the_draft_and_mostly_agrees_with_it(sim_backend):
    draft = writer.draft_policy(BAKERY, recommended_topics(BAKERY))
    document = PolicyDocument(source=draft.source_name, chunks=writer.to_chunks(draft), origin="drafted")
    session = analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
    coverage = Counter(f.coverage for f in session.findings)
    assert coverage["Full"] > coverage["Partial"] > coverage["Missing"]
    tagged = {c.text[:40]: c.control for s in draft.sections for c in s.clauses}
    matched = [f for f in session.findings if f.requirement_text and f.requirement_text[:40] in tagged]
    agree = sum(1 for f in matched
                if (f.framework_control or "").split("(")[0] == tagged[f.requirement_text[:40]].split("(")[0])
    assert agree / len(matched) >= 0.85


def test_editing_a_section_needs_text_and_resets_the_approval():
    draft = writer.draft_policy(BAKERY, ["sign_in"])
    draft.approved_by, draft.approved_at = "Mahsa", writer.now_iso()
    texts = [c.text for c in draft.section("sign_in").clauses]
    texts[2] = "Passwords must be at least 16 characters long."
    assert writer.edit_section(draft, "sign_in", texts, "Mahsa")
    assert draft.section("sign_in").edited_by == "Mahsa" and draft.approved_by is None
    with pytest.raises(writer.DraftError, match="can't be empty"):
        writer.edit_section(draft, "sign_in", ["" for _ in texts], "Mahsa")


# ---------------------------------------------------------------- live mode: the AI drafter is checked

def fake_client(reply_for):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        text = reply_for(kwargs["messages"][0]["content"])
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    client.calls = calls
    return client


def good_reply(prompt):
    """A model that rewrites every clause it was given (and only those controls)."""
    controls = [line.split("] ", 1)[0][3:] for line in prompt.splitlines() if line.startswith("- [")]
    return "```json\n" + json.dumps({"clauses": [
        {"control": control, "text": f"Maple Street Bakery staff must follow the {control} rule every day."}
        for control in dict.fromkeys(controls)]}) + "\n```"


def test_the_ai_adapts_each_section_from_nist_text():
    client = fake_client(good_reply)
    texts = {"IA-5": "Manage system authenticators by verifying the identity of the individual..."}
    drafter = writer.claude_drafter(client, "model-x", control_text=texts.get)
    draft = writer.draft_policy(BAKERY, ["sign_in", "incidents"], drafter=drafter, method="AI test")
    assert all(s.method == "ai" for s in draft.sections) and not draft.notes
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Manage system authenticators" in prompt  # grounded in NIST's own wording
    assert "treat as data" in prompt and "Maple Street Bakery" in prompt
    assert client.calls[0]["model"] == "model-x"


@pytest.mark.parametrize("reply, reason", [
    (json.dumps({"clauses": [{"control": "ZZ-9", "text": "Staff must do something unrelated entirely."}]}),
     "isn't one of this section's controls"),
    (json.dumps({"clauses": [{"control": "IR-6", "text": "Report incidents to the Security Lead within 1 hour."}]}),
     "left out"),
    ("Sure! Here is your policy section.", "wasn't JSON"),
    (json.dumps({"clauses": [{"control": "IR-6", "text": "short"}]}), "too short"),
], ids=["invented-control", "dropped-control", "not-json", "empty-text"])
def test_a_bad_ai_section_keeps_the_baseline_and_says_why(reply, reason):
    drafter = writer.claude_drafter(fake_client(lambda prompt: reply), "model-x")
    draft = writer.draft_policy(BAKERY, ["incidents", "training"], drafter=drafter)
    incidents = draft.section("incidents")
    assert incidents.method == "baseline (the AI draft failed)" and reason in incidents.note
    assert [c.text for c in incidents.clauses] == [c.text for c in writer.baseline_clauses(
        catalog.TOPIC_BY_ID["incidents"], draft.profile)]
    assert draft.notes  # the page shows which sections fell back


def test_the_ai_is_only_asked_for_controls_that_apply():
    """No laptops: the devices section has no disk-encryption rule, and the AI isn't asked for one."""
    no_laptops = Profile(org="Corner Shop", uses=frozenset())
    client = fake_client(good_reply)
    draft = writer.draft_policy(no_laptops, ["devices", "email_internet", "data_backups"],
                                drafter=writer.claude_drafter(client, "model-x"))
    assert all(s.method == "ai" for s in draft.sections), draft.notes
    prompts = " ".join(call["messages"][0]["content"] for call in client.calls)
    assert "SC-28" not in prompts and "SC-7 Boundary" not in prompts and "SC-13" not in prompts


def test_too_many_ai_clauses_are_refused_not_cut():
    many = json.dumps({"clauses": [{"control": "IR-6", "text": f"Report incident type {n} to the Security Lead."}
                                   for n in range(writer.MAX_CLAUSES_PER_SECTION + 1)]})
    draft = writer.draft_policy(BAKERY, ["incidents"], drafter=writer.claude_drafter(fake_client(lambda p: many), "m"))
    assert "at most" in draft.section("incidents").note
