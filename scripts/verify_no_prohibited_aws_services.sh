#!/usr/bin/env bash
# =============================================================================
# Proof that no prohibited AWS service is an active dependency.
#
# Version 1 is explicitly limited to EC2, EBS, S3, IAM, SSM, CloudWatch and
# (optionally) Route 53. This script fails the build if DynamoDB, RDS, RDS
# Proxy, Lambda, SQS, ECS, Fargate, ElastiCache or API Gateway appears as:
#
#   * a Terraform resource or data source,
#   * an IAM action or service principal,
#   * a Python/Node dependency,
#   * an environment variable or compose service,
#   * a boto3 client/resource call in application code.
#
# Documentation is allowed to *mention* them (this file does), so prose files
# are checked only for Terraform-style resource declarations.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; YELLOW=""; RESET=""; }

failures=0
checks=0

# Directories that are never part of the shipped system.
EXCLUDES=(
  --exclude-dir=.git
  --exclude-dir=node_modules
  --exclude-dir=.venv
  --exclude-dir=dist
  --exclude-dir=artifacts
  --exclude-dir=__pycache__
  --exclude-dir=.terraform
  --exclude-dir=.ruff_cache
  --exclude-dir=.pytest_cache
  --exclude-dir=.mypy_cache
)

report() {
  local label="$1" pattern="$2"; shift 2
  checks=$((checks + 1))
  local hits
  if hits="$(grep -rniE "${pattern}" "${EXCLUDES[@]}" "$@" . 2>/dev/null)"; then
    if [ -n "${hits}" ]; then
      echo "${RED}FAIL${RESET} ${label}"
      echo "${hits}" | head -20 | sed 's/^/       /'
      failures=$((failures + 1))
      return
    fi
  fi
  echo "${GREEN}ok  ${RESET} ${label}"
}

echo "Prohibited AWS service check"
echo "============================"

# --- 1. Terraform resources / data sources --------------------------------
report "no prohibited Terraform resources" \
  '^[[:space:]]*(resource|data)[[:space:]]+"aws_(dynamodb|db_instance|db_cluster|db_proxy|rds_[a-z_]*|lambda_[a-z_]*|sqs_[a-z_]*|ecs_[a-z_]*|elasticache_[a-z_]*|api_gateway[a-z_]*|apigatewayv2_[a-z_]*)' \
  --include='*.tf' --include='*.tf.json'

# --- 2. IAM actions and service principals --------------------------------
report "no prohibited IAM actions" \
  '"(dynamodb|rds|rds-db|lambda|sqs|ecs|elasticache|apigateway|execute-api):' \
  --include='*.tf' --include='*.json' --include='*.py'

report "no prohibited service principals" \
  '(lambda|ecs|ecs-tasks|rds|dynamodb|sqs|elasticache|apigateway)\.amazonaws\.com' \
  --include='*.tf' --include='*.json'

# --- 3. SDK usage in application code -------------------------------------
report "no prohibited boto3 clients" \
  "boto3\.(client|resource)\([\"'](dynamodb|rds|rds-data|lambda|sqs|ecs|elasticache|apigateway|apigatewayv2)[\"']" \
  --include='*.py'

report "no prohibited AWS SDK imports" \
  '@aws-sdk/client-(dynamodb|rds|lambda|sqs|ecs|elasticache|api-gateway)' \
  --include='*.ts' --include='*.tsx' --include='*.json' --include='*.mjs'

# --- 4. Declared dependencies ---------------------------------------------
report "no prohibited Python dependencies" \
  '^[[:space:]]*"?(aiobotocore\[dynamodb\]|pynamodb|boto3-stubs\[dynamodb|aws-lambda-powertools|aws-cdk[a-z.-]*lambda|sqlalchemy-redshift)' \
  --include='pyproject.toml' --include='requirements*.txt'

report "no prohibited Node dependencies" \
  '"(aws-lambda|serverless|@aws-cdk/aws-lambda|dynamodb-[a-z-]+)"[[:space:]]*:' \
  --include='package.json'

# --- 5. Compose services and runtime configuration ------------------------
report "no prohibited compose services" \
  '^[[:space:]]{2}(dynamodb|localstack|elasticmq|sqs|redis|elasticache)[a-z-]*:' \
  --include='compose*.yaml' --include='docker-compose*.yml'

report "no prohibited environment variables" \
  '^[[:space:]]*(DYNAMODB|RDS|RDS_PROXY|LAMBDA|SQS|ECS|FARGATE|ELASTICACHE|API_GATEWAY|REDIS)_[A-Z_]+=' \
  --include='*.env' --include='.env.example' --include='*.yaml' --include='*.yml' --include='Makefile'

# --- 6. Redis/ElastiCache client libraries --------------------------------
report "no Redis client libraries" \
  '^[[:space:]]*"?(redis|aioredis|hiredis|ioredis)[">=~]' \
  --include='pyproject.toml' --include='package.json' --include='requirements*.txt'

# --- 7. Positive confirmation of the allowed set --------------------------
echo
echo "Allowed services actually used:"
for service in \
  "aws_instance:EC2" \
  "aws_ebs_volume:EBS" \
  "aws_s3_bucket:S3" \
  "aws_iam_role:IAM" \
  "aws_ssm_parameter:SSM" \
  "aws_cloudwatch:CloudWatch"
do
  pattern="${service%%:*}"
  name="${service##*:}"
  if grep -rqE "${pattern}" infra/terraform --include='*.tf' 2>/dev/null; then
    echo "  ${GREEN}used${RESET}  ${name}"
  else
    echo "  ${YELLOW}absent${RESET} ${name}"
  fi
done

echo
if [ "${failures}" -gt 0 ]; then
  echo "${RED}${failures} of ${checks} prohibited-service checks failed${RESET}"
  exit 1
fi
echo "${GREEN}all ${checks} prohibited-service checks passed${RESET}"
echo "version 1 uses only: EC2, EBS, S3, IAM, SSM, CloudWatch, optional Route 53"
