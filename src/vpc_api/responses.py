"""HTTP response shaping."""

from __future__ import annotations

import json
from typing import Any

from .errors import ApiError

PROBLEM_CONTENT_TYPE = "application/problem+json"
JSON_CONTENT_TYPE = "application/json"

SECURITY_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def _build(status_code: int, body: dict[str, Any], content_type: str, extra: dict[str, str] | None) -> dict[str, Any]:
    """Assemble an API Gateway (payload format 2.0) Lambda response."""
    headers = dict(SECURITY_HEADERS)
    headers["Content-Type"] = content_type
    if extra:
        headers.update(extra)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, separators=(",", ":"), default=str),
    }


def json_response(
    status_code: int,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a successful JSON response."""
    return _build(status_code, body, JSON_CONTENT_TYPE, headers)


def problem_response(error: ApiError, *, instance: str | None = None) -> dict[str, Any]:
    """Render an :class:`~vpc_api.errors.ApiError` as RFC 7807."""
    payload: dict[str, Any] = {
        "type": f"https://docs.example.com/problems/{error.code}",
        "title": error.title,
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
    }
    if instance:
        payload["instance"] = instance
    if error.errors:
        payload["errors"] = error.errors
    return _build(error.status, payload, PROBLEM_CONTENT_TYPE, None)
