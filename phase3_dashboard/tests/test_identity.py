"""Reviewer identity from the company sign-in (Identity-Aware Proxy) instead of a typed name."""

from phase3_dashboard.core import identity

HEADERS = {"X-Goog-Authenticated-User-Email": "accounts.google.com:rashmi@kaygen.com",
           "X-Goog-IAP-JWT-Assertion": "signed.jwt.token"}


def test_off_unless_configured(monkeypatch):
    monkeypatch.delenv("COPILOT_IDENTITY", raising=False)
    assert identity.from_headers(HEADERS) is None and not identity.required()


def test_takes_the_signed_in_email(monkeypatch):
    monkeypatch.setenv("COPILOT_IDENTITY", "iap")
    monkeypatch.delenv("COPILOT_IAP_AUDIENCE", raising=False)
    assert identity.required()
    assert identity.from_headers(HEADERS) == "rashmi@kaygen.com"
    assert identity.from_headers({k.lower(): v for k, v in HEADERS.items()}) == "rashmi@kaygen.com"
    assert identity.from_headers({}) is None  # no sign-in, no decisions


def test_verifies_the_signed_token_when_an_audience_is_set(monkeypatch):
    monkeypatch.setenv("COPILOT_IDENTITY", "iap")
    monkeypatch.setenv("COPILOT_IAP_AUDIENCE", "/projects/123/locations/us-central1/services/copilot")
    seen = {}

    def verify(token, audience):
        seen.update(token=token, audience=audience)
        return {"email": "anu@kaygen.com"}

    monkeypatch.setattr(identity, "verify_jwt", verify)
    assert identity.from_headers(HEADERS) == "anu@kaygen.com"  # the token wins over the plain header
    assert seen == {"token": "signed.jwt.token", "audience": "/projects/123/locations/us-central1/services/copilot"}
    monkeypatch.setattr(identity, "verify_jwt", lambda token, audience: None)
    assert identity.from_headers(HEADERS) is None  # a forged or expired token is no identity


def test_rejects_odd_values(monkeypatch):
    monkeypatch.setenv("COPILOT_IDENTITY", "iap")
    monkeypatch.delenv("COPILOT_IAP_AUDIENCE", raising=False)
    assert identity.from_headers({"X-Goog-Authenticated-User-Email": "accounts.google.com:not-an-email"}) is None
    assert identity.from_headers({"X-Goog-Authenticated-User-Email": "x:" + "a" * 90 + "@b.com"}) is None
