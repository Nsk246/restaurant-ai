#!/usr/bin/env bash
# Runs on every deploy, before the new version takes traffic.
# Fly aborts the release if this exits non-zero, so a broken migration never
# reaches a live phone line.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is not set}"

# Wait for the database before doing anything. On Fly the private route to a
# Postgres machine is not always ready the instant a release machine boots,
# and a cold or restarting database drops the first connection. Failing there
# aborts a deploy for a reason that resolves itself in ten seconds.
echo "==> waiting for the database"
for i in $(seq 1 30); do
  if psql "$DATABASE_URL" -q -c "SELECT 1" >/dev/null 2>&1; then
    echo "    connected after ${i}s"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: no database connection after 30s." >&2
    echo "       Check: fly status -a <postgres-app>" >&2
    psql "$DATABASE_URL" -c "SELECT 1" || true
    exit 1
  fi
  sleep 1
done

echo "==> migrating"
bash db/migrate.sh "$DATABASE_URL"

echo "==> seeding"
for f in db/seed/*.sql; do
  echo "    $f"
  psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -f "$f"
done

# The seeded phone number is a placeholder and the seed is idempotent on
# slug, so re-running never updates it. Drive it from the environment
# instead: a value in a file gets overwritten every time the repo is
# refreshed, and a wrong number means calls resolve no tenant and die.
if [ -n "${RESTAURANT_PHONE:-}" ]; then
  echo "==> setting the inbound number to $RESTAURANT_PHONE"
  psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -c \
    "UPDATE phone_numbers SET e164 = '$RESTAURANT_PHONE'
     WHERE restaurant_id = (SELECT id FROM restaurants WHERE slug='pilot')"
fi
if [ -n "${RESTAURANT_TRANSFER_PHONE:-}" ]; then
  psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -c \
    "UPDATE restaurants SET transfer_phone_e164 = '$RESTAURANT_TRANSFER_PHONE'
     WHERE slug='pilot'"
fi

echo "==> schema assertions"
psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 \
  | sed 's/^.*NOTICE:  //'
