#!/usr/bin/env bash
#
# Build and deploy the stack, then print the outputs you need to use it.
#
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --parameter-overrides ProjectName=my-api
#
set -euo pipefail

STACK_NAME="${STACK_NAME:-vpc-provisioning-api}"
REGION="${AWS_REGION:-eu-west-1}"

cd "$(dirname "$0")/.."

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: '$1' was not found on PATH." >&2
    echo "       Install it with: brew install $2" >&2
    exit 1
  }
}

require sam aws-sam-cli
require aws awscli

echo "==> Validating template"
sam validate --lint --region "$REGION"

echo "==> Building"
sam build

echo "==> Deploying ${STACK_NAME} to ${REGION}"
sam deploy --stack-name "$STACK_NAME" --region "$REGION" "$@"

echo
echo "==> Stack outputs"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

cat <<'EOF'

Next steps
----------
  export API_URL="$(aws cloudformation describe-stacks --stack-name vpc-provisioning-api \
      --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
  export VPC_API_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18) + "Aa1!")')"
  export API_TOKEN="$(python3 scripts/get_token.py --username you@example.com)"
  python3 scripts/smoke_test.py
EOF