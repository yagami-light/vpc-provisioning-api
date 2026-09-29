#!/usr/bin/env python3
"""End-to-end smoke test against a deployed stack."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a deployed stack end to end.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("API_URL"),
        help="API base URL (default: the API_URL environment variable)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("API_TOKEN"),
        help="Cognito ID token (default: the API_TOKEN environment variable)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for provisioning (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser.parse_args(argv)


def call(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Perform one HTTP call and return ``(status, parsed_body)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        return exc.code, payload


class Report:
    """Collects pass/fail lines and makes the exit code reflect them."""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        suffix = f" - {detail}" if detail and not condition else ""
        print(f"[{mark}] {label}{suffix}")
        if not condition:
            self.failures += 1

    def finish(self) -> int:
        if self.failures:
            print(f"\n{self.failures} check(s) failed.")
            return 1
        print("\nAll checks passed.")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_url or not args.token:
        raise SystemExit(
            "Both --api-url and --token are required "
            "(or set the API_URL and API_TOKEN environment variables).\n"
            "Get a token with: python3 scripts/get_token.py --username you@example.com"
        )

    base = args.api_url.rstrip("/")
    report = Report()

    status, _ = call("GET", f"{base}/health", token=None)
    report.check("GET /health is reachable without a token", status == 200, str(status))

    status, _ = call("GET", f"{base}/vpcs", token=None)
    report.check("GET /vpcs without a token is rejected", status == 401, str(status))

    status, _ = call("GET", f"{base}/vpcs", token="not-a-real-token")
    report.check("GET /vpcs with a bogus token is rejected", status == 401, str(status))

    payload = {
        "name": f"smoke-{uuid.uuid4().hex[:8]}",
        "cidrBlock": "10.99.0.0/16",
        "subnets": [
            {"name": "subnet-a", "cidrBlock": "10.99.1.0/24"},
            {"name": "subnet-b", "cidrBlock": "10.99.2.0/24"},
        ],
        "tags": {"purpose": "smoke-test"},
    }
    idempotency_key = f"smoke-{uuid.uuid4()}"

    status, created = call(
        "POST",
        f"{base}/vpcs",
        token=args.token,
        body=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    report.check("POST /vpcs is accepted", status == 202, f"{status} {created}")

    request_id = created.get("requestId", "")
    report.check("the response contains a requestId", bool(request_id))
    if not request_id:
        return report.finish()

    status, replayed = call(
        "POST",
        f"{base}/vpcs",
        token=args.token,
        body=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    report.check("an Idempotency-Key replay returns 200", status == 200, str(status))
    report.check(
        "the replay returns the same requestId",
        replayed.get("requestId") == request_id,
        f"{replayed.get('requestId')} != {request_id}",
    )

    deadline = time.time() + args.timeout
    record: dict[str, Any] = {}
    while time.time() < deadline:
        _, record = call("GET", f"{base}/vpcs/{request_id}", token=args.token)
        if record.get("status") in {"COMPLETED", "FAILED"}:
            break
        print(f"    ... status={record.get('status')}, waiting")
        time.sleep(POLL_INTERVAL_SECONDS)

    report.check(
        "the request reached COMPLETED",
        record.get("status") == "COMPLETED",
        f"status={record.get('status')} error={record.get('error')}",
    )

    resources = record.get("resources") or {}
    report.check("a VPC id was recorded", str(resources.get("vpcId", "")).startswith("vpc-"))
    report.check("both subnets were recorded", len(resources.get("subnets", [])) == 2)
    report.check(
        "every subnet records its availability zone",
        all(subnet.get("availabilityZone") for subnet in resources.get("subnets", [])),
    )

    status, listing = call("GET", f"{base}/vpcs?limit=50", token=args.token)
    report.check(
        "GET /vpcs lists the request",
        status == 200
        and any(item.get("requestId") == request_id for item in listing.get("items", [])),
    )

    status, _ = call("GET", f"{base}/vpcs/{uuid.uuid4()}", token=args.token)
    report.check("GET of an unknown request returns 404", status == 404, str(status))

    status, _ = call("DELETE", f"{base}/vpcs/{request_id}", token=args.token)
    report.check("DELETE /vpcs/{id} is not exposed", status in {403, 404}, str(status))

    vpc_id = resources.get("vpcId")
    print(f"\nCreated requestId: {request_id}")
    if vpc_id:
        print(f"Created vpcId:     {vpc_id}")
        print("Tear it down with:")
        print(f"  python3 scripts/cleanup.py --request-id {request_id} --yes")

    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
