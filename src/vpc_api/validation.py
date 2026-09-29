"""Pre-flight validation."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from .errors import ValidationError
from .models import SubnetSpec, VpcRequest

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TAG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9 _.:/=+\-@]{1,128}$")

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

RESERVED_TAG_KEYS = frozenset({"managedby"})
RESERVED_TAG_PREFIXES = ("aws:", "prov:")

ALLOWED_TOP_LEVEL_FIELDS = frozenset({"name", "cidrBlock", "subnets", "tags"})
ALLOWED_SUBNET_FIELDS = frozenset({"name", "cidrBlock", "availabilityZone"})

MAX_TAG_VALUE_LENGTH = 256
MIN_VPC_PREFIXLEN = 16
MAX_PREFIXLEN = 28


class _Problems:
    """Accumulates field-level problems so all of them can be returned at once."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, field: str, message: str) -> None:
        self.items.append({"field": field, "message": message})

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)


def _reject_unknown_fields(
    payload: dict[str, Any],
    allowed: frozenset[str],
    problems: _Problems,
    *,
    prefix: str = "",
) -> None:
    """Reject unknown fields instead of silently ignoring them."""
    for key in payload:
        if key not in allowed:
            problems.add(f"{prefix}{key}", "unknown field is not allowed")


def _parse_cidr(
    raw: Any,
    field: str,
    problems: _Problems,
    *,
    require_private: bool,
) -> ipaddress.IPv4Network | None:
    """Parse and range-check a CIDR block, recording any problem found."""
    if not isinstance(raw, str):
        problems.add(field, "must be a string in CIDR notation, e.g. 10.0.0.0/16")
        return None
    try:
        network = ipaddress.IPv4Network(raw, strict=True)
    except ValueError as exc:
        problems.add(field, f"is not a valid IPv4 CIDR block ({exc})")
        return None
    if network.prefixlen > MAX_PREFIXLEN:
        problems.add(field, f"prefix length must be no longer than /{MAX_PREFIXLEN}")
    if require_private and not network.is_private:
        problems.add(field, "must be an RFC 1918 private range (10/8, 172.16/12 or 192.168/16)")
    return network


def _validate_tags(raw: Any, problems: _Problems) -> dict[str, str]:
    """Validate the optional user tag map."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.add("tags", "must be an object mapping tag keys to string values")
        return {}

    accepted: dict[str, str] = {}
    for key, value in raw.items():
        field = f"tags.{key}"
        if not isinstance(key, str) or not TAG_KEY_PATTERN.match(key):
            problems.add(field, "key must be 1-128 characters from a safe alphabet")
            continue
        lowered = key.lower()
        if lowered in RESERVED_TAG_KEYS or lowered.startswith(RESERVED_TAG_PREFIXES):
            problems.add(field, "key is reserved by the service and cannot be set by callers")
            continue
        if not isinstance(value, str) or len(value) > MAX_TAG_VALUE_LENGTH:
            problems.add(
                field,
                f"value must be a string of at most {MAX_TAG_VALUE_LENGTH} characters",
            )
            continue
        accepted[key] = value
    return accepted


def _validate_subnets(
    raw: Any,
    vpc_network: ipaddress.IPv4Network | None,
    problems: _Problems,
    *,
    max_subnets: int,
) -> list[SubnetSpec]:
    """Validate the subnet array, including containment and overlap rules."""
    if not isinstance(raw, list):
        problems.add("subnets", "must be an array")
        return []
    if not raw:
        problems.add("subnets", "must contain at least one subnet")
        return []
    if len(raw) > max_subnets:
        problems.add("subnets", f"must contain at most {max_subnets} subnets")

    specs: list[SubnetSpec] = []
    seen_names: set[str] = set()
    seen_networks: list[tuple[int, ipaddress.IPv4Network]] = []

    for index, item in enumerate(raw):
        prefix = f"subnets[{index}]"
        if not isinstance(item, dict):
            problems.add(prefix, "must be an object")
            continue
        _reject_unknown_fields(item, ALLOWED_SUBNET_FIELDS, problems, prefix=f"{prefix}.")

        name = item.get("name")
        name_is_valid = isinstance(name, str) and bool(NAME_PATTERN.match(name))
        if not name_is_valid:
            problems.add(f"{prefix}.name", "must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        elif name.lower() in seen_names:
            problems.add(f"{prefix}.name", f"'{name}' is already used by another subnet")
        else:
            seen_names.add(name.lower())

        network = _parse_cidr(
            item.get("cidrBlock"),
            f"{prefix}.cidrBlock",
            problems,
            require_private=True,
        )
        if network is not None:
            if vpc_network is not None:
                if network == vpc_network:
                    problems.add(
                        f"{prefix}.cidrBlock",
                        "must be strictly smaller than the VPC CIDR block",
                    )
                elif not network.subnet_of(vpc_network):
                    problems.add(
                        f"{prefix}.cidrBlock",
                        f"must be contained within the VPC CIDR block {vpc_network}",
                    )
            for other_index, other_network in seen_networks:
                if network.overlaps(other_network):
                    problems.add(
                        f"{prefix}.cidrBlock",
                        f"overlaps subnets[{other_index}].cidrBlock ({other_network})",
                    )
            seen_networks.append((index, network))

        availability_zone = item.get("availabilityZone")
        az_is_valid = availability_zone is None or isinstance(availability_zone, str)
        if not az_is_valid:
            problems.add(f"{prefix}.availabilityZone", "must be a string")

        if name_is_valid and network is not None and az_is_valid:
            specs.append(
                SubnetSpec(
                    name=name,
                    cidr_block=str(network),
                    availability_zone=availability_zone,
                )
            )

    return specs


def validate_request_document(payload: Any, *, max_subnets: int = 16) -> VpcRequest:
    """Validate a decoded request body and return the normalised model."""
    problems = _Problems()

    if not isinstance(payload, dict):
        raise ValidationError(
            "The request body must be a JSON object.",
            errors=[{"field": "body", "message": "must be a JSON object"}],
        )

    _reject_unknown_fields(payload, ALLOWED_TOP_LEVEL_FIELDS, problems)

    name = payload.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        problems.add("name", f"must match {NAME_PATTERN.pattern}")

    vpc_network = _parse_cidr(payload.get("cidrBlock"), "cidrBlock", problems, require_private=True)
    if vpc_network is not None and vpc_network.prefixlen < MIN_VPC_PREFIXLEN:
        problems.add("cidrBlock", f"prefix length must be no shorter than /{MIN_VPC_PREFIXLEN}")

    subnets = _validate_subnets(payload.get("subnets"), vpc_network, problems, max_subnets=max_subnets)

    tags = _validate_tags(payload.get("tags"), problems)

    if problems:
        raise ValidationError(
            f"{len(problems)} validation error(s) found; all of them are listed in 'errors'.",
            errors=problems.items,
        )

    return VpcRequest(
        name=name,
        cidr_block=str(vpc_network),
        subnets=subnets,
        tags=tags,
    )


def validate_idempotency_key(value: Any) -> str | None:
    """Validate the optional ``Idempotency-Key`` header value."""
    if value is None:
        return None
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_PATTERN.match(value):
        raise ValidationError(
            "The Idempotency-Key header is invalid.",
            errors=[
                {
                    "field": "Idempotency-Key",
                    "message": "must be 1-128 characters from [A-Za-z0-9._:-]",
                }
            ],
        )
    return value
