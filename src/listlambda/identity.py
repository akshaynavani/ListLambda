"""Resolve the caller's identity from an API Gateway HTTP API event."""

from __future__ import annotations

import re
from typing import Any

from .config import get_settings
from .errors import UnauthorizedError

USER_HEADER = "x-user-id"
_USER_ID_RE = re.compile(r"^[A-Za-z0-9@._:\-]{1,128}$")


def resolve_owner_id(event: dict[str, Any]) -> str:
    """Return the owner id for the request.

    Preference order:
      1. ``sub`` claim from a JWT authorizer (Cognito or any OIDC provider).
      2. ``X-User-Id`` header, only when ALLOW_HEADER_IDENTITY is enabled (dev / load tests).
    """
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if sub:
        return _validated(str(sub))

    if get_settings().allow_header_identity:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        header_id = headers.get(USER_HEADER)
        if header_id:
            return _validated(header_id)

    raise UnauthorizedError("Missing caller identity")


def _validated(owner_id: str) -> str:
    if not _USER_ID_RE.match(owner_id):
        raise UnauthorizedError("Caller identity is malformed")
    return owner_id
