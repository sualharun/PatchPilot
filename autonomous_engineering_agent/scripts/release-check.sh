#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

env_file=".env"
production="false"
skip_tests="false"
skip_docker_image="false"
skip_database="false"

usage() {
  cat <<'EOF'
Usage: scripts/release-check.sh [options]

Runs the checks PatchPilot should pass before local beta or VPS deployment.

Options:
  --env-file PATH       Environment file to load. Default: .env
  --production          Run doctor with PATCHPILOT_PRODUCTION=true
  --skip-tests          Skip pytest and ruff checks
  --skip-docker-image   Skip the Docker image execution check
  --skip-database       Skip database reachability check
  -h, --help            Show this help

Examples:
  scripts/release-check.sh --env-file .env.ci.example --skip-docker-image
  scripts/release-check.sh --env-file .env --production
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --production)
      production="true"
      shift
      ;;
    --skip-tests)
      skip_tests="true"
      shift
      ;;
    --skip-docker-image)
      skip_docker_image="true"
      shift
      ;;
    --skip-database)
      skip_database="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$env_file" ]]; then
  echo "Missing env file: $env_file" >&2
  echo "Copy .env.vps.example to .env and fill in real values for deployment." >&2
  exit 1
fi

run_step() {
  local name="$1"
  shift
  echo
  echo "==> $name"
  "$@"
}

doctor_args=(python3 -m agent.cli doctor --env-file "$env_file")
if [[ "$skip_docker_image" != "true" ]]; then
  doctor_args+=(--check-docker-run)
fi
if [[ "$skip_database" == "true" ]]; then
  doctor_args+=(--skip-database)
fi

if [[ "$production" == "true" ]]; then
  run_step "Production preflight" env PATCHPILOT_PRODUCTION=true "${doctor_args[@]}"
else
  run_step "Local preflight" "${doctor_args[@]}"
fi

run_step "Database migration check" python3 -m agent.cli migrate --env-file "$env_file"

if [[ "$skip_tests" != "true" ]]; then
  run_step "Unit tests" env \
    DASHBOARD_AUTH_ENABLED=false \
    DASHBOARD_DEMO_DATA_ENABLED=true \
    DASHBOARD_SECURE_COOKIES=false \
    GITHUB_OAUTH_MOCK_ENABLED=false \
    PATCHPILOT_PRODUCTION=false \
    AGENT_MODEL_PRICES_JSON= \
    python3 -m pytest
  run_step "Lint" python3 -m ruff check .
fi

run_step "Docker Compose config" bash -c 'PATCHPILOT_ENV_FILE="$1" docker compose --env-file "$1" -f docker-compose.yml config >/dev/null' _ "$env_file"
run_step "VPS Docker Compose config" bash -c 'PATCHPILOT_ENV_FILE="$1" docker compose --env-file "$1" -f docker-compose.yml -f docker-compose.vps.yml config >/dev/null' _ "$env_file"

echo
echo "Release checks completed."
