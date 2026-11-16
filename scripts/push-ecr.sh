#!/usr/bin/env bash
# Build and push API + web images to ECR.
#
# Reads AWS_REGION, ECR_API_REPO_URL, and ECR_WEB_REPO_URL from Pulumi stack
# outputs in infra/ unless overridden by env or flags.
#
# Optional:
#   AWS_PROFILE         — AWS CLI profile (e.g. trader-ecr)
#   PULUMI_STACK        — stack name (default: current selected stack)
#   IMAGE_TAG           — defaults to "latest"; also tags with short git sha when available
#   DOCKER_PLATFORM     — defaults to linux/amd64 (for Linux x86_64 hosts)
#   AWS_REGION / ECR_API_REPO_URL / ECR_WEB_REPO_URL — override Pulumi outputs
#
# Example:
#   export AWS_PROFILE=trader-ecr
#   ./scripts/push-ecr.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${ROOT}/infra"
cd "$ROOT"

IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
ECR_API_REPO_URL="${ECR_API_REPO_URL:-}"
ECR_WEB_REPO_URL="${ECR_WEB_REPO_URL:-}"
PULUMI_STACK="${PULUMI_STACK:-}"

usage() {
  cat <<'EOF'
Usage: ./scripts/push-ecr.sh [--stack STACK] [--region REGION] [--api-url URL] [--web-url URL] [--tag TAG] [--platform PLATFORM]

Builds the API and web Docker images (default platform: linux/amd64) and pushes them to ECR.
Region and repository URLs default to Pulumi stack outputs from infra/.

Environment:
  AWS_PROFILE, PULUMI_STACK, IMAGE_TAG, DOCKER_PLATFORM
  AWS_REGION, ECR_API_REPO_URL, ECR_WEB_REPO_URL (optional overrides)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack) PULUMI_STACK="$2"; shift 2 ;;
    --region) AWS_REGION="$2"; shift 2 ;;
    --api-url) ECR_API_REPO_URL="$2"; shift 2 ;;
    --web-url) ECR_WEB_REPO_URL="$2"; shift 2 ;;
    --tag) IMAGE_TAG="$2"; shift 2 ;;
    --platform) DOCKER_PLATFORM="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if ! command -v pulumi >/dev/null 2>&1; then
  echo "pulumi CLI is required to read stack outputs." >&2
  exit 1
fi

if [[ ! -d "$INFRA_DIR" ]]; then
  echo "Expected Pulumi project at ${INFRA_DIR}" >&2
  exit 1
fi

pulumi_output() {
  local name="$1"
  if [[ -n "$PULUMI_STACK" ]]; then
    pulumi -C "$INFRA_DIR" stack output "$name" --stack "$PULUMI_STACK"
  else
    pulumi -C "$INFRA_DIR" stack output "$name"
  fi
}

echo "Reading ECR settings from Pulumi stack${PULUMI_STACK:+ (${PULUMI_STACK})}..."
if [[ -z "$AWS_REGION" ]]; then
  AWS_REGION="$(pulumi_output awsRegion)"
fi
if [[ -z "$ECR_API_REPO_URL" ]]; then
  ECR_API_REPO_URL="$(pulumi_output apiRepositoryUrl)"
fi
if [[ -z "$ECR_WEB_REPO_URL" ]]; then
  ECR_WEB_REPO_URL="$(pulumi_output webRepositoryUrl)"
fi

if [[ -z "$AWS_REGION" || -z "$ECR_API_REPO_URL" || -z "$ECR_WEB_REPO_URL" ]]; then
  echo "Failed to resolve AWS_REGION / ECR_API_REPO_URL / ECR_WEB_REPO_URL from Pulumi." >&2
  echo "Run 'pulumi up' in infra/ first, or pass --region / --api-url / --web-url." >&2
  exit 1
fi

REGISTRY="${ECR_API_REPO_URL%%/*}"

echo "Logging in to ECR registry ${REGISTRY} (${AWS_REGION})..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

GIT_SHA=""
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
  GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
fi

echo "Building API image (${DOCKER_PLATFORM})..."
docker build --platform "$DOCKER_PLATFORM" -t "trader-api:${IMAGE_TAG}" -f Dockerfile .

echo "Building web image (${DOCKER_PLATFORM})..."
docker build --platform "$DOCKER_PLATFORM" -t "trader-web:${IMAGE_TAG}" -f web/Dockerfile web

tag_and_push() {
  local local_name="$1"
  local repo_url="$2"
  docker tag "${local_name}:${IMAGE_TAG}" "${repo_url}:${IMAGE_TAG}"
  docker push "${repo_url}:${IMAGE_TAG}"
  if [[ -n "$GIT_SHA" && "$IMAGE_TAG" != "sha-${GIT_SHA}" ]]; then
    docker tag "${local_name}:${IMAGE_TAG}" "${repo_url}:sha-${GIT_SHA}"
    docker push "${repo_url}:sha-${GIT_SHA}"
  fi
}

echo "Pushing API → ${ECR_API_REPO_URL}:${IMAGE_TAG}"
tag_and_push trader-api "$ECR_API_REPO_URL"

echo "Pushing web → ${ECR_WEB_REPO_URL}:${IMAGE_TAG}"
tag_and_push trader-web "$ECR_WEB_REPO_URL"

echo
echo "Done. Set on the Linux box (.env):"
echo "  ECR_API_IMAGE=${ECR_API_REPO_URL}:${IMAGE_TAG}"
echo "  ECR_WEB_IMAGE=${ECR_WEB_REPO_URL}:${IMAGE_TAG}"
if [[ -n "$GIT_SHA" ]]; then
  echo "Also pushed: :sha-${GIT_SHA}"
fi
