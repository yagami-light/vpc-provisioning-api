#!/usr/bin/env python3
"""Create a Cognito user (idempotently) and print an ID token."""

from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_STACK = "vpc-provisioning-api"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Cognito user idempotently and print an ID token.",
    )
    parser.add_argument("--username", required=True, help="user's email address")
    parser.add_argument(
        "--stack",
        default=DEFAULT_STACK,
        help=f"CloudFormation stack to read outputs from (default: {DEFAULT_STACK})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1",
        help="AWS region (default: AWS_REGION or eu-west-1)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("VPC_API_PASSWORD"),
        help="password to set; defaults to the VPC_API_PASSWORD environment variable",
    )
    parser.add_argument(
        "--print-endpoint",
        action="store_true",
        help="also print the API base URL, ready to paste into curl",
    )
    return parser.parse_args(argv)


def stack_outputs(stack: str, region: str) -> dict[str, str]:
    """Read a stack's outputs as a plain ``{key: value}`` mapping."""
    client = boto3.client("cloudformation", region_name=region)
    try:
        response = client.describe_stacks(StackName=stack)
    except ClientError as exc:
        raise SystemExit(f"Could not describe stack {stack!r} in {region}: {exc}") from exc

    outputs = response["Stacks"][0].get("Outputs", [])
    return {item["OutputKey"]: item["OutputValue"] for item in outputs}


def ensure_user(cognito, user_pool_id: str, username: str, password: str) -> None:
    """Create the user if needed, then set a permanent password."""
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        print(f"created user {username}", file=sys.stderr)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "UsernameExistsException":
            raise
        print(f"user {username} already exists", file=sys.stderr)

    cognito.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def fetch_id_token(cognito, client_id: str, username: str, password: str) -> str:
    """Authenticate with USER_PASSWORD_AUTH and return the ID token."""
    response = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    authentication = response.get("AuthenticationResult") or {}
    token = authentication.get("IdToken")
    if not token:
        raise SystemExit(
            "Authentication did not return an ID token. "
            "A NEW_PASSWORD_REQUIRED challenge usually means the password was not set permanently."
        )
    return token


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.password:
        raise SystemExit(
            "No password supplied. Export VPC_API_PASSWORD or pass --password.\n"
            "Hint: export VPC_API_PASSWORD=\"$(python3 -c "
            "'import secrets; print(secrets.token_urlsafe(18) + \"Aa1!\")')\""
        )

    outputs = stack_outputs(args.stack, args.region)

    missing = [key for key in ("UserPoolId", "UserPoolClientId") if key not in outputs]
    if missing:
        raise SystemExit(
            f"Stack {args.stack!r} is missing these outputs: {', '.join(missing)}.\n"
            "Is it the stack this project deployed?"
        )

    cognito = boto3.client("cognito-idp", region_name=args.region)
    ensure_user(cognito, outputs["UserPoolId"], args.username, args.password)
    token = fetch_id_token(cognito, outputs["UserPoolClientId"], args.username, args.password)

    if args.print_endpoint:
        base = outputs.get("ApiUrl", "")
        print(f"# API_URL={base}")
        print(f"# export API_URL={base}", file=sys.stderr)

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
