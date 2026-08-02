#!/usr/bin/env bash
# Proves a Postgres backup is actually restorable, instead of trusting that pg_dump exiting 0
# means the data is recoverable. Restores the most recent (or a given) backup into a scratch
# database -- never into the live "patchpilot" database -- and asserts a known table exists and
# is queryable. Meant to run on a schedule (see deploy/systemd/patchpilot-backup-verify.timer);
# any failure here means the last backup cannot actually be trusted, so this fails loud
# (non-zero exit, message on stdout) for cron mail/monitoring to catch.
set -euo pipefail

cd "$(dirname "$0")/.."

backup_dir="${BACKUP_DIR:-backups}"
scratch_db="${VERIFY_SCRATCH_DB:-patchpilot_restore_check}"
compose="docker compose -f docker-compose.yml -f docker-compose.vps.yml"

backup_file="${1:-}"
if [[ -z "$backup_file" ]]; then
  backup_file="$(ls -1t "$backup_dir"/patchpilot-postgres-*.sql.gz 2>/dev/null | head -n1 || true)"
fi

if [[ -z "$backup_file" || ! -f "$backup_file" ]]; then
  echo "BACKUP VERIFY FAILED: no backup file found (looked in $backup_dir, arg was '${1:-}')"
  exit 1
fi

echo "Verifying backup: $backup_file"

cleanup() {
  $compose exec -T postgres psql -U patchpilot -d postgres \
    -c "DROP DATABASE IF EXISTS $scratch_db;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! $compose exec -T postgres psql -U patchpilot -d postgres \
    -c "DROP DATABASE IF EXISTS $scratch_db;"; then
  echo "BACKUP VERIFY FAILED: could not drop pre-existing scratch database $scratch_db"
  exit 1
fi

if ! $compose exec -T postgres psql -U patchpilot -d postgres \
    -c "CREATE DATABASE $scratch_db OWNER patchpilot;"; then
  echo "BACKUP VERIFY FAILED: could not create scratch database $scratch_db"
  exit 1
fi

if ! gzip -dc "$backup_file" | $compose exec -T postgres psql -U patchpilot -d "$scratch_db" -v ON_ERROR_STOP=1; then
  echo "BACKUP VERIFY FAILED: restoring $backup_file into $scratch_db failed"
  exit 1
fi

found_table="$($compose exec -T postgres psql -U patchpilot -d "$scratch_db" -tAc "SELECT to_regclass('public.runs');")"
found_table="$(echo "$found_table" | tr -d '[:space:]')"

if [[ "$found_table" != "runs" ]]; then
  echo "BACKUP VERIFY FAILED: expected table 'runs' not found after restoring $backup_file"
  exit 1
fi

row_count="$($compose exec -T postgres psql -U patchpilot -d "$scratch_db" -tAc "SELECT count(*) FROM runs;")"
row_count="$(echo "$row_count" | tr -d '[:space:]')"

echo "BACKUP VERIFY OK: $backup_file restored into $scratch_db, runs table present ($row_count rows)"
