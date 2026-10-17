"""Webhook request authentication."""

import hashlib
import hmac

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

_PREFIX = "sha256="


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Check Meta's `X-Hub-Signature-256` header against the raw request body.

    Meta signs every webhook POST: header value is `sha256=<hex>` where <hex> is
    HMAC-SHA256(key=App Secret, msg=exact raw body bytes). It must be computed over the
    bytes as received - re-serialising parsed JSON would change whitespace/escaping and
    break the match. `compare_digest` keeps the comparison constant-time.
    """
    if not signature_header or not signature_header.startswith(_PREFIX):
        return False
    received = signature_header[len(_PREFIX):].strip()
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def verify_tasks_token(authorization: str | None, audience: str, service_account: str) -> bool:
    """Check the OIDC token Cloud Tasks attaches to worker requests.

    Header: `Authorization: Bearer <JWT>`. google-auth verifies the JWT signature against
    Google's public certs, the issuer (accounts.google.com) and the audience; we then
    require the token to belong to our Cloud Tasks service account. Blocking (may fetch
    certs) - call it from a threadpool.
    """
    if not authorization or not authorization.startswith("Bearer ") or not service_account:
        return False
    try:
        claims = id_token.verify_oauth2_token(authorization[len("Bearer "):], google_requests.Request(), audience)
    except ValueError:
        return False
    return bool(claims.get("email_verified")) and claims.get("email") == service_account


def verify_admin(authorization: str | None, api_key: str | None, audience: str, allowed_emails: frozenset[str]) -> bool:
    """Admin endpoints: "Bearer <ADMIN_API_KEY>" (constant-time compare), or a Google ID token
    (audience = this service's URL) whose verified email is in ADMIN_ALLOWED_EMAILS. Blocking - use a threadpool."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[len("Bearer "):].strip()
    if api_key and hmac.compare_digest(supplied.encode(), api_key.encode()):
        return True
    if not allowed_emails or not audience or supplied.count(".") != 2:  # not a JWT
        return False
    try:
        claims = id_token.verify_oauth2_token(supplied, google_requests.Request(), audience)
    except ValueError:
        return False
    return bool(claims.get("email_verified")) and str(claims.get("email", "")).lower() in allowed_emails


def tokens_match(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison for the GET handshake verify token."""
    if supplied is None:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
