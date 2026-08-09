#!/usr/bin/env bash
# Runs on every deploy, before the new version takes traffic.
# Fly aborts the release if this exits non-zero, so a broken migration never
# reaches a live phone line.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is not set}"

echo "==> migrating"
bash db/migrate.sh "$DATABASE_URL"

echo "==> seeding"
for f in db/seed/*.sql; do
  echo "    $f"
  psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> schema assertions"
psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 \
  | sed 's/^.*NOTICE:  //'
