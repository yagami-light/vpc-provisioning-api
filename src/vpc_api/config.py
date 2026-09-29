"""Runtime configuration and lazily-cached AWS clients."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass

import boto3
from botocore.client import BaseClient


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process environment."""

    project_name: str
    table_name: str
    queue_url: str
    max_subnets_per_request: int
    max_attempts: int
    max_body_bytes: int
    log_level: str
    region: str

    @property
    def managed_by_tag(self) -> str:
        """Value written to the ``ManagedBy`` tag on every resource we create."""
        return self.project_name


def _load_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1"
    return Settings(
        project_name=os.environ.get("PROJECT_NAME", "vpc-provisioning-api"),
        table_name=os.environ.get("TABLE_NAME", "Requests"),
        queue_url=os.environ.get("QUEUE_URL", ""),
        max_subnets_per_request=_env_int("MAX_SUBNETS_PER_REQUEST", 16),
        max_attempts=_env_int("MAX_ATTEMPTS", 3),
        max_body_bytes=_env_int("MAX_BODY_BYTES", 64 * 1024),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        region=region,
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` for this process."""
    return _load_settings()


@functools.lru_cache(maxsize=1)
def get_ec2_client() -> BaseClient:
    """EC2 client used for all network provisioning calls."""
    return boto3.client("ec2", region_name=get_settings().region)


@functools.lru_cache(maxsize=1)
def get_sqs_client() -> BaseClient:
    """SQS client used to enqueue and consume provisioning jobs."""
    return boto3.client("sqs", region_name=get_settings().region)


@functools.lru_cache(maxsize=1)
def _get_dynamodb_resource():
    """DynamoDB *resource* (not client) so items are plain dicts."""
    return boto3.resource("dynamodb", region_name=get_settings().region)


def get_table():
    """Return the single DynamoDB table used for request records."""
    return _get_dynamodb_resource().Table(get_settings().table_name)


def reset_caches() -> None:
    """Drop cached settings and clients."""
    get_settings.cache_clear()
    get_ec2_client.cache_clear()
    get_sqs_client.cache_clear()
    _get_dynamodb_resource.cache_clear()
