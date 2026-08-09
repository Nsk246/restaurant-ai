#!/usr/bin/env bash
# Apply migrations that have not been applied yet, in filename order.
#
# Why this exists: `psql -f` on every file every time means an already-applied
# migration errors out. Today those errors are recognisable. By migration 006
# they are noise, and a real failure hides inside it.
#
# Each migration wraps itself in BEGIN/COMMIT, which is checked below. We do
# not pass --single-transaction: the file's own COMMIT would close the outer
# transaction early, so the flag implies a protection it does not give.
#
# Adopting a database that predates this runner: run with --baseline once.
# That records every current migration as applied WITHOUT executing it, which
# is what you want when the schema is already there. It is opt-in because
# guessing wrong would either skip a real migration or re-run a destructive
# one, and neither is something a script should decide on your behalf.
#
# Usage:
#   db/migrate.sh [DATABASE_URL]
#   db/migrate.sh --baseline [DATABASE_URL]
set -euo pipefail

BASELINE=0
if [ "${1:-}" = "--baseline" ]; then
  BASELINE=1
  shift
fi

DB="${1:-${DATABASE_URL:-postgresql://operator:operator@127.0.0.1:5432/operator}}"
DIR="$(cd "$(dirname "$0")" && pwd)"

psql "$DB" -q -v ON_ERROR_STOP=1 <<'SQL'
SET client_min_messages = warning;
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
SQL

# An existing schema with no tracking table means this runner is new here.
# Stop and say so rather than re-running 001 against a live schema, which
# fails on the first CREATE TYPE and looks like a broken migration.
if [ "$BASELINE" -eq 0 ]; then
  tracked="$(psql "$DB" -tAc "SELECT count(*) FROM schema_migrations")"
  has_schema="$(psql "$DB" -tAc \
    "SELECT count(*) FROM information_schema.tables
     WHERE table_schema='public' AND table_name='restaurants'")"
  if [ "$tracked" = "0" ] && [ "$has_schema" != "0" ]; then
    echo "This database already has a schema but no migration history." >&2
    echo "" >&2
    echo "If it is up to date with db/migrations, adopt it:" >&2
    echo "    bash db/migrate.sh --baseline" >&2
    echo "" >&2
    echo "If you are not sure, rebuild instead:  make reset" >&2
    exit 1
  fi
fi

applied=0
skipped=0

for f in "$DIR"/migrations/*.sql; do
  name="$(basename "$f")"
  sum="$(sha256sum "$f" | cut -d' ' -f1)"

  recorded="$(psql "$DB" -tAc \
    "SELECT checksum FROM schema_migrations WHERE filename = '$name'")"

  if [ -n "$recorded" ]; then
    if [ "$recorded" != "$sum" ]; then
      echo "ERROR: $name changed after it was applied." >&2
      echo "       Add a new migration rather than editing this one." >&2
      exit 1
    fi
    skipped=$((skipped + 1))
    continue
  fi

  if ! grep -qiE '^[[:space:]]*BEGIN[[:space:]]*;' "$f" \
     || ! grep -qiE '^[[:space:]]*COMMIT[[:space:]]*;' "$f"; then
    echo "ERROR: $name must wrap itself in BEGIN; ... COMMIT;" >&2
    echo "       Without it a failure leaves the schema half-applied." >&2
    exit 1
  fi

  if [ "$BASELINE" -eq 1 ]; then
    echo "  baseline $name (recorded, not executed)"
  else
    echo "  applying $name"
    psql "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
  fi
  psql "$DB" -q -v ON_ERROR_STOP=1 \
    -c "INSERT INTO schema_migrations (filename, checksum) VALUES ('$name', '$sum')"
  applied=$((applied + 1))
done

if [ "$BASELINE" -eq 1 ]; then
  echo "baselined $applied migrations. Verify with: make test"
else
  echo "migrations: $applied applied, $skipped already current"
fi
