"""Error taxonomy."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Base class for every error that becomes an HTTP response."""

    status: int = 500
    code: str = "internal_error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.errors = errors or []


class ValidationError(ApiError):
    """The request body is syntactically or semantically invalid."""

    status = 400
    code = "validation_failed"
    title = "Request Validation Failed"


class UnauthorizedError(ApiError):
    """No usable identity was present on the request."""

    status = 401
    code = "unauthorized"
    title = "Unauthorized"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "A valid bearer token is required.")


class NotFoundError(ApiError):
    """The requested resource does not exist (or is not visible to the caller)."""

    status = 404
    code = "not_found"
    title = "Not Found"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "The requested resource does not exist.")


class ConflictError(ApiError):
    """The request conflicts with the current state of the resource."""

    status = 409
    code = "conflict"
    title = "Conflict"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "The request conflicts with the current state.")


class InternalError(ApiError):
    """Unexpected failure."""

    status = 500
    code = "internal_error"
    title = "Internal Server Error"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "An unexpected error occurred.")


class ProvisioningError(Exception):
    """Base class for failures raised while talking to the EC2 API."""

    transient: bool = False
    code: str = "provisioning_failed"

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code


class TransientError(ProvisioningError):
    """A retryable condition: throttling, a 5xx from EC2, a network blip."""

    transient = True
    code = "transient_error"


class QuotaError(ProvisioningError):
    """An account limit was hit (VPC or subnet limits)."""

    transient = False
    code = "quota_exceeded"


class ProvisioningFailure(ProvisioningError):
    """A permanent, non-retryable failure (bad input, missing permission)."""

    transient = False
    code = "permanent_failure"


class IllegalTransitionError(Exception):
    """An attempt was made to move a request along a state edge that the state"""


TRANSIENT_ERROR_CODES = frozenset(
    {
        "RequestLimitExceeded",
        "RequestThrottled",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "Unavailable",
        "DependencyViolation",
    }
)

QUOTA_ERROR_CODES = frozenset(
    {
        "VpcLimitExceeded",
        "SubnetLimitExceeded",
        "ResourceLimitExceeded",
        "InsufficientFreeAddressesInSubnet",
    }
)


def classify_ec2_error(error_code: str, detail: str) -> ProvisioningError:
    """Map a ``botocore`` EC2 error code onto the right provisioning error."""
    if error_code in TRANSIENT_ERROR_CODES:
        return TransientError(detail, code=error_code)
    if error_code in QUOTA_ERROR_CODES:
        return QuotaError(detail, code=error_code)
    return ProvisioningFailure(detail, code=error_code or "provisioning_failed")
