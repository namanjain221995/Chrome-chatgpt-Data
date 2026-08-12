#!/usr/bin/env bash
# Fail if application/runtime files introduce a prohibited AWS managed service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

failures=0
scan() {
  local label="$1" pattern="$2"; shift 2
  local hits
  hits="$(grep -rniE "${pattern}" \
    --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
    --exclude-dir=dist --exclude-dir=artifacts --exclude-dir=__pycache__ \
    --exclude=verify_no_prohibited_aws_services.sh "$@" 2>/dev/null || true)"
  if [ -n "${hits}" ]; then
    printf 'FAIL %s\n%s\n' "${label}" "${hits}" >&2
    failures=$((failures + 1))
  else
    printf 'ok   %s\n' "${label}"
  fi
}

scan "SDK clients" \
  "boto3\.(client|resource)\([\"'](dynamodb|rds|rds-data|lambda|sqs|ecs|elasticache|apigateway|apigatewayv2)[\"']" \
  services apps
scan "IAM actions and principals" \
  '"(dynamodb|rds|rds-db|lambda|sqs|ecs|elasticache|apigateway|execute-api):|(lambda|ecs|ecs-tasks|rds|dynamodb|sqs|elasticache|apigateway)\.amazonaws\.com' \
  services apps deploy scripts compose.yaml compose.prod.yaml .github
scan "runtime service declarations" \
  '^[[:space:]]{2}(dynamodb|localstack|elasticmq|sqs|redis|elasticache)[a-z-]*:' \
  compose.yaml compose.prod.yaml .github
scan "runtime environment variables" \
  '^[[:space:]]*(DYNAMODB|RDS|RDS_PROXY|LAMBDA|SQS|ECS|FARGATE|ELASTICACHE|API_GATEWAY|REDIS)_[A-Z_]+=' \
  .env.example compose.yaml compose.prod.yaml services apps
scan "client dependencies" \
  'aioredis|hiredis|ioredis|pynamodb|aws-lambda-powertools|@aws-sdk/client-(dynamodb|rds|lambda|sqs|ecs|elasticache|api-gateway)' \
  services/backend/pyproject.toml apps/chrome-extension/package.json

[ "${failures}" -eq 0 ] || exit 1
echo "prohibited AWS service scan passed"
