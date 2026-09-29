"""Asynchronous provisioning worker - the provisioning plane."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..config import get_settings
from ..errors import ProvisioningError, TransientError
from ..models import RequestRecord
from ..provisioning import NetworkProvisioner
from ..repository import (
    claim_request,
    complete_request,
    fail_request,
    get_request,
    release_for_retry,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SQS entry point.  Returns a partial-batch-failure response."""
    failures: list[dict[str, str]] = []

    for sqs_record in event.get("Records", []):
        message_id = sqs_record.get("messageId", "unknown")
        try:
            _process(sqs_record)
        except Exception:  # noqa: BLE001
            logger.exception(
                "provisioning attempt failed; the message will be redelivered messageId=%s",
                message_id,
            )
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


def _process(sqs_record: dict[str, Any]) -> None:
    """Handle a single SQS message."""
    payload = json.loads(sqs_record["body"])
    request_id = payload["requestId"]

    record = get_request(request_id)
    if record is None:
        logger.warning("no stored record for this message; skipping requestId=%s", request_id)
        return

    if record.is_terminal:
        logger.info("request already finished; skipping requestId=%s status=%s", request_id, record.status.value)
        return

    claimed = claim_request(request_id)
    if claimed is None:
        logger.info("request was already claimed by another worker; skipping requestId=%s", request_id)
        return

    provisioner = NetworkProvisioner()
    try:
        resources = provisioner.provision(claimed)
    except ProvisioningError as error:
        _handle_failure(claimed, provisioner, error)
        return
    except Exception as error:  # noqa: BLE001
        _handle_failure(claimed, provisioner, TransientError(str(error)))
        return

    if not complete_request(request_id, resources):
        logger.warning("could not mark the request COMPLETED (it moved on already) requestId=%s", request_id)
        return

    logger.info(
        "provisioning completed requestId=%s vpcId=%s subnetCount=%s",
        request_id, resources.vpc_id, len(resources.subnets),
    )


def _handle_failure(
    record: RequestRecord,
    provisioner: NetworkProvisioner,
    error: ProvisioningError,
) -> None:
    """Apply the retry policy to a failed attempt."""
    attempts = record.attempts
    max_attempts = get_settings().max_attempts

    if error.transient and attempts < max_attempts:
        release_for_retry(record.request_id, error.detail)
        logger.warning(
            "transient failure; released for retry requestId=%s code=%s attempts=%s maxAttempts=%s",
            record.request_id, error.code, attempts, max_attempts,
        )
        raise error

    reason = "retries exhausted" if error.transient else "non-retryable error"
    logger.warning(
        "rolling back after failure requestId=%s code=%s attempts=%s reason=%s",
        record.request_id, error.code, attempts, reason,
    )
    deleted = provisioner.rollback(record.request_id)
    fail_request(record.request_id, error.detail, code=error.code)
    logger.info("request marked FAILED and rolled back requestId=%s deletedCount=%s", record.request_id, len(deleted))
