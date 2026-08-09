#!/usr/bin/env bash
# Runs once when the container is created. Every wait in here is bounded:
# a setup script that can hang forever is worse than one that fails loudly.
set -euo pipefail

echo "==> installing postgres client"
# The base image has no psql or pg_isready. Without this the wait loop below
# spins forever, because a missing command returns non-zero just like a
# database that is not ready yet. That is what stuck the first Codespace.
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends postgresql-client

echo "==> installing python deps"
pip install --quiet --upgrade pip
pip install --quiet -r services/voice/requirements.txt
pip install --quiet pytest pytest-asyncio ruff

echo "==> waiting for postgres"
for i in $(seq 1 60); do
  if pg_isready -h db -U operator -q; then break; fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: postgres did not come up in 60s." >&2
    echo "Check: docker compose -f .devcontainer/docker-compose.yml logs db" >&2
    exit 1
  fi
  sleep 1
done

export PGPASSWORD=operator
DB="postgresql://operator:operator@db:5432/operator"

echo "==> applying migrations and seed"
for f in db/migrations/*.sql db/seed/*.sql; do
  echo "    $f"
  psql "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> schema assertions"
psql "$DB" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 | sed 's/^.*NOTICE:  //'

[ -f .env ] || cp .env.example .env
sed -i 's#^DATABASE_URL=.*#DATABASE_URL=postgresql://operator:operator@db:5432/operator#' .env
sed -i 's#^REDIS_URL=.*#REDIS_URL=redis://redis:6379/0#' .env

cat <<'MSG'

Ready.

  make test     schema assertions + python tests
  make api      voice service on :8000

Twilio webhook target, once port 8000 is public:
  https://$CODESPACE_NAME-8000.app.github.dev/twilio/voice

If Twilio reports error 31920 the Codespaces relay is rejecting the
WebSocket upgrade. That is not fixable from inside the container.
Deploy to Fly and point Twilio there. See SETUP.md.

MSG
