"""Domain models and the request lifecycle state machine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .errors import IllegalTransitionError


class Status(str, Enum):
    """The four states of a provisioning request."""

    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.PROVISIONING, Status.FAILED}),
    Status.PROVISIONING: frozenset({Status.PENDING, Status.COMPLETED, Status.FAILED}),
    Status.COMPLETED: frozenset(),
    Status.FAILED: frozenset(),
}

TERMINAL_STATES = frozenset({Status.COMPLETED, Status.FAILED})


def can_transition(source: Status | str, target: Status | str) -> bool:
    """Return ``True`` when ``source -> target`` is a declared edge."""
    source_status = Status(source)
    target_status = Status(target)
    return target_status in ALLOWED_TRANSITIONS.get(source_status, frozenset())


def assert_transition(source: Status | str, target: Status | str) -> None:
    """Raise :class:`IllegalTransitionError` unless the edge is declared."""
    if not can_transition(source, target):
        raise IllegalTransitionError(f"Illegal state transition: {source} -> {target}")


@dataclass(frozen=True)
class SubnetSpec:
    """One requested subnet."""

    name: str
    cidr_block: str
    availability_zone: str | None = None

    @property
    def logical_id(self) -> str:
        return self.name

    def to_api_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "logicalId": self.logical_id,
            "cidrBlock": self.cidr_block,
        }
        if self.availability_zone:
            payload["availabilityZone"] = self.availability_zone
        return payload

    @classmethod
    def from_api_dict(cls, payload: dict[str, Any]) -> SubnetSpec:
        return cls(
            name=payload["name"],
            cidr_block=payload["cidrBlock"],
            availability_zone=payload.get("availabilityZone"),
        )


@dataclass(frozen=True)
class VpcRequest:
    """A normalised, already-validated network definition."""

    name: str
    cidr_block: str
    subnets: list[SubnetSpec]
    tags: dict[str, str] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cidrBlock": self.cidr_block,
            "subnets": [subnet.to_api_dict() for subnet in self.subnets],
            "tags": dict(self.tags),
        }

    @classmethod
    def from_api_dict(cls, payload: dict[str, Any]) -> VpcRequest:
        return cls(
            name=payload["name"],
            cidr_block=payload["cidrBlock"],
            subnets=[SubnetSpec.from_api_dict(item) for item in payload["subnets"]],
            tags=dict(payload.get("tags") or {}),
        )


@dataclass(frozen=True)
class SubnetResource:
    """A subnet that really exists in EC2, bound back to its logical id."""

    logical_id: str
    name: str
    subnet_id: str
    cidr_block: str
    availability_zone: str | None

    def to_api_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "logicalId": self.logical_id,
            "name": self.name,
            "subnetId": self.subnet_id,
            "cidrBlock": self.cidr_block,
        }
        if self.availability_zone:
            payload["availabilityZone"] = self.availability_zone
        return payload

    @classmethod
    def from_api_dict(cls, payload: dict[str, Any]) -> SubnetResource:
        return cls(
            logical_id=payload["logicalId"],
            name=payload["name"],
            subnet_id=payload["subnetId"],
            cidr_block=payload["cidrBlock"],
            availability_zone=payload.get("availabilityZone"),
        )


@dataclass(frozen=True)
class ResourceSet:
    """Everything the worker built for one request."""

    vpc_id: str
    vpc_cidr_block: str
    subnets: list[SubnetResource]

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "vpcId": self.vpc_id,
            "vpcCidrBlock": self.vpc_cidr_block,
            "subnets": [subnet.to_api_dict() for subnet in self.subnets],
        }

    @classmethod
    def from_api_dict(cls, payload: dict[str, Any]) -> ResourceSet:
        return cls(
            vpc_id=payload["vpcId"],
            vpc_cidr_block=payload["vpcCidrBlock"],
            subnets=[SubnetResource.from_api_dict(item) for item in payload.get("subnets", [])],
        )


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RequestRecord:
    """One provisioning request, as stored."""

    request_id: str
    status: Status
    request: VpcRequest
    owner_sub: str
    owner_email: str | None = None
    attempts: int = 0
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    idempotency_key: str | None = None
    body_hash: str | None = None
    resources: ResourceSet | None = None
    error: dict[str, Any] | None = None
    deleted_at: str | None = None
    deleted_resources: list[str] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        request: VpcRequest,
        owner_sub: str,
        owner_email: str | None = None,
        idempotency_key: str | None = None,
        body_hash: str | None = None,
    ) -> RequestRecord:
        """Create a fresh ``PENDING`` record with a new ``requestId``."""
        created_at = utcnow_iso()
        return cls(
            request_id=str(uuid.uuid4()),
            status=Status.PENDING,
            request=request,
            owner_sub=owner_sub,
            owner_email=owner_email,
            created_at=created_at,
            updated_at=created_at,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def to_api_dict(self) -> dict[str, Any]:
        """Serialise to the public API shape."""
        payload: dict[str, Any] = {
            "requestId": self.request_id,
            "status": self.status.value,
            "request": self.request.to_api_dict(),
            "attempts": self.attempts,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.resources is not None:
            payload["resources"] = self.resources.to_api_dict()
        if self.error is not None:
            payload["error"] = self.error
        if self.deleted_at is not None:
            payload["deletedAt"] = self.deleted_at
            payload["deletedResources"] = list(self.deleted_resources)
        if self.idempotency_key:
            payload["idempotencyKey"] = self.idempotency_key
        return payload

    @classmethod
    def from_api_dict(cls, payload: dict[str, Any]) -> RequestRecord:
        resources = payload.get("resources")
        return cls(
            request_id=payload["requestId"],
            status=Status(payload["status"]),
            request=VpcRequest.from_api_dict(payload["request"]),
            owner_sub=payload.get("ownerSub", ""),
            owner_email=payload.get("ownerEmail"),
            attempts=int(payload.get("attempts", 0)),
            created_at=payload.get("createdAt") or utcnow_iso(),
            updated_at=payload.get("updatedAt") or utcnow_iso(),
            idempotency_key=payload.get("idempotencyKey"),
            body_hash=payload.get("bodyHash"),
            resources=ResourceSet.from_api_dict(resources) if resources else None,
            error=payload.get("error"),
            deleted_at=payload.get("deletedAt"),
            deleted_resources=list(payload.get("deletedResources") or []),
        )
