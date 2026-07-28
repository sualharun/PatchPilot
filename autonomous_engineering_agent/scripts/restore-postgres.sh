#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 backups/patchpilot-postgres-YYYYMMDDTHHMMSSZ.sql.gz" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

gzip -dc "$1" | docker compose -f docker-compose.yml -f docker-compose.vps.yml exec -T postgres \
  psql -U patchpilot patchpilot
