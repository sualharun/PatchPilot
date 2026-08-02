#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

backup_dir="${BACKUP_DIR:-backups}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
mkdir -p "$backup_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/patchpilot-postgres-$timestamp.sql.gz"

docker compose -f docker-compose.yml -f docker-compose.vps.yml exec -T postgres \
  pg_dump -U patchpilot patchpilot | gzip > "$output"

if [[ ! -s "$output" ]]; then
  echo "BACKUP FAILED: $output is empty" >&2
  rm -f "$output"
  exit 1
fi

echo "Wrote $output"

if [[ "$retention_days" -gt 0 ]]; then
  find "$backup_dir" -maxdepth 1 -name 'patchpilot-postgres-*.sql.gz' -mtime "+$retention_days" -print -delete \
    | sed 's/^/Pruned old backup: /'
fi
