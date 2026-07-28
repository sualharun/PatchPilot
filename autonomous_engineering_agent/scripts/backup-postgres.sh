#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

backup_dir="${BACKUP_DIR:-backups}"
mkdir -p "$backup_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/patchpilot-postgres-$timestamp.sql.gz"

docker compose -f docker-compose.yml -f docker-compose.vps.yml exec -T postgres \
  pg_dump -U patchpilot patchpilot | gzip > "$output"

echo "Wrote $output"
