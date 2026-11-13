"""Anonymous per-browser identity (SPEC section 2, "a signed random session token").

The API issues a random ID signed with SESSION_KEY; the browser keeps the token and sends it as
"Authorization: Bearer <token>". Every Lambda verifies the signature before using the ID as owner_id, so an ID
made up by the client is never trusted. There is no expiry: losing the token (clearing browser data) loses access.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from shared.secrets import setting

VERSION = "v1"
PREFIX = "b-"        # owner_id of a browser identity; local runs use "local-dev"


def _sign(sid: str, key: str) -> str:
    return hmac.new(key.encode(), f"{VERSION}.{sid}".encode(), hashlib.sha256).hexdigest()[:40]


def issue(key: str | None = None) -> tuple[str, str]:
    """A new (owner_id, token)."""
    sid = secrets.token_urlsafe(18)
    return PREFIX + sid, f"{VERSION}.{sid}.{_sign(sid, key or setting('SESSION_KEY'))}"


def verify(token: str | None, key: str | None = None) -> str | None:
    """The owner_id for a valid token, else None."""
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != VERSION or not parts[1] or len(parts[1]) > 64:
        return None
    if not hmac.compare_digest(parts[2], _sign(parts[1], key or setting("SESSION_KEY"))):
        return None
    return PREFIX + parts[1]


def from_headers(headers: dict) -> str | None:
    """owner_id from an "Authorization: Bearer <token>" header (header names in any case)."""
    value = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), "") or ""
    return verify(value[7:].strip()) if value.lower().startswith("bearer ") else None
