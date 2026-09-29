#!/usr/bin/env python3
"""Out-of-band teardown for networks this service created."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vpc_api import repository  # noqa: E402
from vpc_api.config import reset_caches  # noqa: E402
from vpc_api.provisioning import (  # noqa: E402
    TAG_MANAGED_BY,
    TAG_REQUEST_ID,
    NetworkProvisioner,
)

UNKNOWN_REQUEST = "-"
DEFAULT_PROJECT = "vpc-provisioning-api"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def tags_of(item: dict[str, Any]) -> dict[str, str]:
    """Return an EC2 resource's tags as a plain mapping."""
    return {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}


def group_by_request(listing: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, list[str]]]:
    """Group the listing by the ``prov:requestId`` that owns each resource."""
    grouped: dict[str, dict[str, list[str]]] = {}

    for resource_key, id_key in (("vpcs", "VpcId"), ("subnets", "SubnetId")):
        for item in listing.get(resource_key, []):
            request_id = tags_of(item).get(TAG_REQUEST_ID, UNKNOWN_REQUEST)
            grouped.setdefault(request_id, {}).setdefault(resource_key, []).append(item[id_key])

    return grouped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List or delete the networks this service owns.",
    )
    parser.add_argument("--list", action="store_true", help="show what exists and exit")
    parser.add_argument(
        "--request-id",
        action="append",
        default=[],
        help="delete one request's network (repeatable)",
    )
    parser.add_argument("--all", action="store_true", help="delete every network this service owns")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm a destructive operation; required with --all or --request-id",
    )
    parser.add_argument(
        "--project-name",
        default=os.environ.get("PROJECT_NAME"),
        help="ManagedBy value to match (default: PROJECT_NAME or vpc-provisioning-api)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="AWS region (default: AWS_REGION)",
    )
    parser.add_argument(
        "--table-name",
        default=os.environ.get("TABLE_NAME"),
        help="DynamoDB table holding request records (default: <project-name>-requests)",
    )
    return parser.parse_args(argv)


def show(listing: dict[str, list[dict[str, Any]]], project: str) -> int:
    """Print what exists, grouped by request.  Returns the number of networks."""
    grouped = group_by_request(listing)
    if not grouped:
        print(f"No resources tagged {TAG_MANAGED_BY}={project} were found.")
        return 0

    print(f"Resources tagged {TAG_MANAGED_BY}={project}:")
    for request_id in sorted(grouped):
        resources = grouped[request_id]
        print(f"\n  requestId: {request_id}")
        for resource_key in ("vpcs", "subnets"):
            for resource_id in resources.get(resource_key, []):
                print(f"    {resource_key:<18} {resource_id}")
    print(f"\n{len(grouped)} network(s).")
    return len(grouped)


def delete(provisioner: NetworkProvisioner, request_ids: list[str]) -> None:
    """Roll each request back, then record that it was torn down."""
    for request_id in request_ids:
        print(f"deleting {request_id} ... ", end="", flush=True)
        deleted = provisioner.rollback(request_id)
        note = mark_record_deleted(request_id, deleted)
        print(f"removed {len(deleted)} resource(s); {note}")


def mark_record_deleted(request_id: str, deleted: list[str]) -> str:
    """Stamp the request record with ``deletedAt``."""
    try:
        if repository.mark_deleted(request_id, deleted):
            return "record marked deleted"
        return "record already marked, or absent"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not mark the request record requestId=%s exceptionType=%s",
            request_id,
            type(exc).__name__,
        )
        return f"record NOT marked ({type(exc).__name__})"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    project = args.project_name or os.environ.get("PROJECT_NAME") or DEFAULT_PROJECT
    os.environ["PROJECT_NAME"] = project
    os.environ["TABLE_NAME"] = args.table_name or f"{project}-requests"
    if args.region:
        os.environ["AWS_REGION"] = args.region
    reset_caches()

    provisioner = NetworkProvisioner()
    listing = provisioner.list_owned_resources()

    if args.list or not (args.request_id or args.all):
        show(listing, project)
        if not (args.request_id or args.all):
            return 0

    if not args.yes:
        raise SystemExit("Refusing to delete without --yes.")

    request_ids = list(args.request_id)
    if args.all:
        request_ids = sorted(group_by_request(listing))
        if not request_ids:
            print("Nothing to delete.")
            return 0

    delete(provisioner, request_ids)
    print("Done.  Request records were kept and stamped with deletedAt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
