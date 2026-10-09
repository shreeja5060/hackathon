"""Who is reviewing: a typed name, or the company account a person signed in with.

The ledger records reviewer names as given, so on a laptop or in a demo anyone can type
any name. Deployed on Cloud Run behind Google's Identity-Aware Proxy (IAP), every request
carries the signed-in user's identity, and the dashboard takes the reviewer from there
and locks the name field. Then a person can't type someone else's name to confirm their
own decision, and "who approved this" in the audit trail is a verified account.

    COPILOT_IDENTITY=iap       use IAP's identity. Only safe when the service can be reached
                               through IAP alone (Cloud Run ingress restricted / IAP enabled),
                               otherwise the plain header could be forged.
    COPILOT_IAP_AUDIENCE=...   also verify IAP's signed JWT for this audience (recommended):
                               Cloud Run with IAP: /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE
                               behind a load balancer: /projects/PROJECT_NUMBER/global/backendServices/ID

Any failure returns None, so the dashboard falls back to asking for a name only when
IAP is not configured; with IAP configured but no valid identity, decisions stay locked.
"""

from __future__ import annotations

import os

IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key"
MAX_IDENTITY_CHARS = 80


def mode() -> str:
    return os.getenv("COPILOT_IDENTITY", "").strip().lower()


def required() -> bool:
    return mode() == "iap"


def _get(headers, name: str) -> str | None:
    if not headers:
        return None
    for key in (name, name.lower(), name.title()):
        try:
            value = headers.get(key)
        except Exception:  # noqa: BLE001 - header containers differ between Streamlit versions
            value = None
        if value:
            return str(value)
    return None


def _clean(email) -> str | None:
    text = " ".join(str(email or "").split())
    if not text or len(text) > MAX_IDENTITY_CHARS or not text.isprintable() or "@" not in text:
        return None
    return text


def verify_jwt(token: str, audience: str) -> dict | None:
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_token(token, google_requests.Request(), audience=audience, certs_url=IAP_CERTS)
    except Exception:  # noqa: BLE001 - an unverifiable token is no identity at all
        return None


def from_headers(headers) -> str | None:
    """The signed-in reviewer's email, or None (not configured, missing or not verifiable)."""
    if not required():
        return None
    audience = os.getenv("COPILOT_IAP_AUDIENCE", "").strip()
    if audience:
        token = _get(headers, "X-Goog-IAP-JWT-Assertion")
        claims = verify_jwt(token, audience) if token else None
        return _clean((claims or {}).get("email"))
    raw = _get(headers, "X-Goog-Authenticated-User-Email")  # "accounts.google.com:alex@example.com"
    return _clean(raw.split(":", 1)[-1]) if raw else None
