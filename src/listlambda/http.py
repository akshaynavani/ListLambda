"""Helpers for building consistent JSON HTTP responses."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types

from .errors import ListLambdaError


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "value"):  # Enum
        return value.value
    raise TypeError(f"Unserialisable value: {type(value)!r}")


def json_response(status_code: int, body: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(
        status_code=status_code,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, default=_default, separators=(",", ":")),
        headers=headers or {},
    )


def error_response(exc: ListLambdaError, request_id: str | None = None) -> Response:
    body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    if request_id:
        body["error"]["request_id"] = request_id
    return json_response(exc.status_code, body)


def parse_limit(raw: str | None, default: int, maximum: int) -> int:
    from .errors import ValidationError

    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError("limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise ValidationError(f"limit must be between 1 and {maximum}")
    return value
