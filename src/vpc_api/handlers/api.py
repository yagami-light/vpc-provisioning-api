"""Synchronous API Lambda - the control plane."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from .. import __version__
from ..config import get_settings, get_sqs_client
from ..errors import (
    ApiError,
    ConflictError,
    InternalError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from ..models import RequestRecord
from ..repository import (
    create_request,
    fail_pending,
    find_by_idempotency_key,
    get_request,
    list_requests,
)
from ..responses import json_response, problem_response
from ..validation import validate_idempotency_key, validate_request_document

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100

ROUTE_HEALTH = "GET /health"
ROUTE_CREATE = "POST /vpcs"
ROUTE_LIST = "GET /vpcs"
ROUTE_GET = "GET /vpcs/{requestId}"


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """API Gateway entry point."""
    instance = event.get("rawPath") or (
        event.get("requestContext", {}).get("http", {}) or {}
    ).get("path")

    try:
        return _dispatch(event)
    except ApiError as exc:
        logger.warning(
            "request rejected path=%s status=%s code=%s detail=%s",
            instance,
            exc.status,
            exc.code,
            exc.detail,
        )
        return problem_response(exc, instance=instance)
    except Exception:  # noqa: BLE001
        logger.exception("unhandled error while processing request path=%s", instance)
        return problem_response(InternalError(), instance=instance)


def _dispatch(event: dict[str, Any]) -> dict[str, Any]:
    """Route the request."""
    route_key = _route_key(event)

    if route_key == ROUTE_HEALTH:
        return _health()
    if route_key == ROUTE_CREATE:
        return _create_vpc(event)
    if route_key == ROUTE_LIST:
        return _list_vpcs(event)
    if route_key == ROUTE_GET:
        return _get_vpc(event)

    raise NotFoundError(f"No route matches {route_key!r}.")


def _route_key(event: dict[str, Any]) -> str:
    """Normalise an event to a ``"METHOD /path"`` route key."""
    route_key = event.get("routeKey")
    if route_key and route_key != "$default":
        return str(route_key)

    http = event.get("requestContext", {}).get("http", {}) or {}
    method = http.get("method", "")
    path = http.get("path") or event.get("rawPath") or ""
    if not method or not path:
        return "$default"
    if path.startswith("/vpcs/"):
        return f"{method} /vpcs/{{requestId}}"
    return f"{method} {path}"


def _principal(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the caller identity from the JWT authorizer context."""
    authorizer = (event.get("requestContext", {}).get("authorizer", {}) or {})
    claims = (authorizer.get("jwt", {}) or {}).get("claims", {}) or {}

    subject = claims.get("sub")
    if not subject:
        raise UnauthorizedError()

    return {
        "sub": subject,
        "email": claims.get("email"),
        "username": claims.get("cognito:username"),
    }


def _header(event: dict[str, Any], name: str) -> str | None:
    """Case-insensitive header lookup."""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _read_body(event: dict[str, Any]) -> dict[str, Any]:
    """Return the decoded JSON body, enforcing the 64 KB cap before parsing."""
    raw = event.get("body")
    if not raw:
        raise ValidationError(
            "A JSON request body is required.",
            errors=[{"field": "body", "message": "must not be empty"}],
        )
    if event.get("isBase64Encoded"):
        raise ValidationError(
            "Binary request bodies are not supported.",
            errors=[{"field": "body", "message": "must be UTF-8 JSON, not base64"}],
        )

    max_bytes = get_settings().max_body_bytes
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValidationError(
            f"The request body must be at most {max_bytes} bytes.",
            errors=[{"field": "body", "message": f"exceeds the {max_bytes} byte limit"}],
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"The request body is not valid JSON ({exc.msg}).",
            errors=[{"field": "body", "message": f"invalid JSON at position {exc.pos}"}],
        ) from exc

    return payload


def _body_hash(payload: dict[str, Any]) -> str:
    """Stable fingerprint of a request body, used to detect idempotency abuse."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_limit(event: dict[str, Any]) -> int:
    """Read and bound the ``limit`` query string parameter."""
    raw = (event.get("queryStringParameters") or {}).get("limit")
    if raw is None or raw == "":
        return DEFAULT_LIST_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "The limit query parameter must be an integer.",
            errors=[{"field": "limit", "message": "must be an integer"}],
        ) from exc
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValidationError(
            f"The limit query parameter must be between 1 and {MAX_LIST_LIMIT}.",
            errors=[
                {"field": "limit", "message": f"must be between 1 and {MAX_LIST_LIMIT}"}
            ],
        )
    return limit


def _health() -> dict[str, Any]:
    """Liveness probe."""
    settings = get_settings()
    return json_response(
        200,
        {
            "status": "ok",
            "service": settings.project_name,
            "version": __version__,
        },
    )


def _create_vpc(event: dict[str, Any]) -> dict[str, Any]:
    """``POST /vpcs`` - validate, record, enqueue, return 202."""
    principal = _principal(event)
    payload = _read_body(event)
    document = validate_request_document(
        payload,
        max_subnets=get_settings().max_subnets_per_request,
    )
    idempotency_key = validate_idempotency_key(_header(event, "Idempotency-Key"))
    body_hash = _body_hash(payload)

    if idempotency_key:
        existing = find_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.body_hash != body_hash:
                raise ConflictError(
                    "This Idempotency-Key was already used with a different request body."
                )
            return json_response(200, existing.to_api_dict())

    record = RequestRecord.new(
        request=document,
        owner_sub=principal["sub"],
        owner_email=principal["email"],
        idempotency_key=idempotency_key,
        body_hash=body_hash,
    )
    if not create_request(record):
        raise ConflictError("A request with this identifier already exists.")

    try:
        enqueue(record)
    except Exception:  # noqa: BLE001
        logger.exception("could not enqueue provisioning job requestId=%s", record.request_id)
        fail_pending(record.request_id, "Could not enqueue the provisioning job.")
        raise InternalError("The request could not be queued for provisioning.") from None

    logger.info("accepted provisioning request requestId=%s", record.request_id)
    return json_response(202, record.to_api_dict())


def enqueue(record: RequestRecord) -> None:
    """Hand a job to SQS."""
    get_sqs_client().send_message(
        QueueUrl=get_settings().queue_url,
        MessageBody=json.dumps(
            {"requestId": record.request_id, "requestedAt": record.created_at},
            separators=(",", ":"),
        ),
    )


def _list_vpcs(event: dict[str, Any]) -> dict[str, Any]:
    """``GET /vpcs`` - list requests, newest first."""
    _principal(event)
    limit = _parse_limit(event)
    records = list_requests(limit)
    return json_response(
        200,
        {
            "items": [record.to_api_dict() for record in records],
            "count": len(records),
        },
    )


def _get_vpc(event: dict[str, Any]) -> dict[str, Any]:
    """``GET /vpcs/{requestId}`` - read one request and its resource ids."""
    _principal(event)
    request_id = (event.get("pathParameters") or {}).get("requestId")
    if not request_id:
        raise NotFoundError("No request id was supplied in the path.")

    record = get_request(request_id)
    if record is None:
        raise NotFoundError(f"No provisioning request with id {request_id!r}.")

    return json_response(200, record.to_api_dict())
