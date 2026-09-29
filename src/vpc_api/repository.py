"""DynamoDB persistence for request records."""

from __future__ import annotations

import logging
import re
from typing import Any

from botocore.exceptions import ClientError

from .config import get_table
from .models import RequestRecord, ResourceSet, Status, utcnow_iso
from .models import assert_transition as _assert_transition

logger = logging.getLogger(__name__)

LIST_PARTITION = "REQUEST"

IDEMPOTENCY_PREFIX = "IDEMPOTENCY#"

def _expression_names(*expressions: str) -> dict[str, str]:
    """Map the ``#alias`` placeholders used by the expressions to attribute names."""
    aliases = {alias for expression in expressions for alias in re.findall(r"#\w+", expression)}
    return {alias: alias[1:] for alias in aliases}


def request_key(request_id: str) -> dict[str, str]:
    """Primary key of the record for ``request_id``."""
    return {"pk": f"REQUEST#{request_id}", "sk": "META"}


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove ``None`` values."""
    return {key: value for key, value in payload.items() if value is not None}


def to_item(record: RequestRecord) -> dict[str, Any]:
    """Serialise a :class:`RequestRecord` into a DynamoDB item."""
    item = _drop_none(
        {
            "requestId": record.request_id,
            "status": record.status.value,
            "request": record.request.to_api_dict(),
            "resources": record.resources.to_api_dict() if record.resources else None,
            "attempts": record.attempts,
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
            "ownerSub": record.owner_sub,
            "ownerEmail": record.owner_email,
            "idempotencyKey": record.idempotency_key,
            "bodyHash": record.body_hash,
            "error": record.error,
            "gsi1pk": LIST_PARTITION,
            "gsi1sk": record.created_at,
            "gsi2pk": f"{IDEMPOTENCY_PREFIX}{record.idempotency_key}" if record.idempotency_key else None,
            "gsi2sk": record.created_at if record.idempotency_key else None,
        }
    )
    if record.deleted_at is not None:
        item["deletedAt"] = record.deleted_at
        if record.deleted_resources:
            item["deletedResources"] = list(record.deleted_resources)
    item.update(request_key(record.request_id))
    return item


def from_item(item: dict[str, Any]) -> RequestRecord:
    """Deserialise a DynamoDB item into a :class:`RequestRecord`."""
    payload = dict(item)
    payload["requestId"] = payload.get("requestId") or str(payload.get("pk", "")).removeprefix("REQUEST#")
    return RequestRecord.from_api_dict(payload)


def get_request(request_id: str) -> RequestRecord | None:
    """Fetch one record by id, or ``None`` when it does not exist."""
    response = get_table().get_item(Key=request_key(request_id))
    item = response.get("Item")
    return from_item(item) if item else None


def find_by_idempotency_key(idempotency_key: str) -> RequestRecord | None:
    """Fetch the record previously created with ``idempotency_key``."""
    response = get_table().query(
        IndexName="gsi2",
        KeyConditionExpression="gsi2pk = :pk",
        ExpressionAttributeValues={":pk": f"{IDEMPOTENCY_PREFIX}{idempotency_key}"},
        Limit=1,
    )
    items = response.get("Items", [])
    return from_item(items[0]) if items else None


def list_requests(limit: int = 20) -> list[RequestRecord]:
    """Return up to ``limit`` records, newest first."""
    response = get_table().query(
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :pk",
        ExpressionAttributeValues={":pk": LIST_PARTITION},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [from_item(item) for item in response.get("Items", [])]


def _is_conditional_failure(exc: ClientError) -> bool:
    """True when a failed write lost a race rather than actually erroring."""
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def create_request(record: RequestRecord) -> bool:
    """Insert a new record, returning ``False`` if the id already exists."""
    try:
        get_table().put_item(
            Item=to_item(record),
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            logger.info("request already exists requestId=%s", record.request_id)
            return False
        raise


def _conditional_update(
    request_id: str,
    *,
    expected: Status,
    update_expression: str,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply a status change if the record is still in ``expected``.

    Returns the updated item, or ``None`` when the write lost a race.
    """
    try:
        response = get_table().update_item(
            Key=request_key(request_id),
            UpdateExpression=update_expression,
            ConditionExpression="#status = :expected",
            ExpressionAttributeNames=_expression_names(update_expression, "#status"),
            ExpressionAttributeValues={":expected": expected.value, **values},
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except ClientError as exc:
        if _is_conditional_failure(exc):
            logger.info("conditional update lost a race requestId=%s expected=%s", request_id, expected.value)
            return None
        raise


def _transition(
    request_id: str,
    *,
    expected: Status,
    target: Status,
    extra_set: str = "",
    values: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Move a record between two states, only if it is still in ``expected``."""
    _assert_transition(expected, target)
    return _conditional_update(
        request_id,
        expected=expected,
        update_expression=f"SET #status = :new, #updatedAt = :now{extra_set}",
        values={":new": target.value, ":now": utcnow_iso(), **(values or {})},
    )


def claim_request(request_id: str) -> RequestRecord | None:
    """``PENDING -> PROVISIONING``, returning the claimed record or ``None``."""
    attributes = _transition(
        request_id,
        expected=Status.PENDING,
        target=Status.PROVISIONING,
        extra_set=", #attempts = if_not_exists(#attempts, :zero) + :one",
        values={":zero": 0, ":one": 1},
    )
    return from_item(attributes) if attributes else None


def release_for_retry(request_id: str, detail: str) -> bool:
    """``PROVISIONING -> PENDING`` after a transient failure."""
    return (
        _transition(
            request_id,
            expected=Status.PROVISIONING,
            target=Status.PENDING,
            extra_set=", #error = :error",
            values={":error": {"code": "transient_error", "detail": detail}},
        )
        is not None
    )


def complete_request(request_id: str, resources: ResourceSet) -> bool:
    """``PROVISIONING -> COMPLETED`` and attach the real AWS resource ids."""
    return (
        _transition(
            request_id,
            expected=Status.PROVISIONING,
            target=Status.COMPLETED,
            extra_set=", #resources = :resources REMOVE #error",
            values={":resources": resources.to_api_dict()},
        )
        is not None
    )


def fail_request(
    request_id: str,
    detail: str,
    *,
    code: str = "provisioning_failed",
    expected: Status = Status.PROVISIONING,
) -> bool:
    """Move ``expected`` to ``FAILED``, recording why."""
    return (
        _transition(
            request_id,
            expected=expected,
            target=Status.FAILED,
            extra_set=", #error = :error",
            values={":error": {"code": code, "detail": detail}},
        )
        is not None
    )


def fail_pending(request_id: str, detail: str) -> bool:
    """``PENDING -> FAILED`` when the job could not even be handed to SQS."""
    return fail_request(request_id, detail, code="enqueue_failed", expected=Status.PENDING)


def mark_deleted(request_id: str, deleted_resources: list[str] | None = None) -> bool:
    """Record that an operator tore this network down out of band."""
    names = {"#deletedAt": "deletedAt", "#updatedAt": "updatedAt"}
    values: dict[str, Any] = {":now": utcnow_iso()}
    update_expression = "SET #deletedAt = :now, #updatedAt = :now"

    if deleted_resources:
        names["#deletedResources"] = "deletedResources"
        values[":resources"] = list(deleted_resources)
        update_expression += ", #deletedResources = :resources"

    try:
        get_table().update_item(
            Key=request_key(request_id),
            UpdateExpression=update_expression,
            ConditionExpression="attribute_exists(pk) AND attribute_not_exists(#deletedAt)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            logger.info("record already marked deleted requestId=%s", request_id)
            return False
        raise
