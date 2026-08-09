#!/usr/bin/env bash
# Runs once, when the container is created.
# Every wait is bounded. A setup script that can hang forever is worse than
# one that fails loudly.
set -euo pipefail

echo "==> installing postgres and redis"
# The base image ships a Yarn apt source whose signing key is not present.
# `apt-get update` then fails, and under `set -e` that kills the whole script
# before anything useful happens. We do not need Yarn, so drop the source and
# update only what we can. `|| true` because a single unreachable mirror must
# not block a setup that only needs the Debian repos.
sudo rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources
sudo apt-get update -qq || true
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  postgresql postgresql-contrib redis-server

echo "==> starting services"
bash "$(dirname "$0")/start.sh"

echo "==> creating role and database"
# `sudo -u postgres` prompts for a password here. Devcontainers give the
# vscode user passwordless sudo to root only, and switching to a third user
# falls outside that rule. Going through root with `su` stays inside it.
pg() { sudo su postgres -c "$1"; }
pg "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='operator'\"" | grep -q 1 || \
  pg "psql -q -c \"CREATE ROLE operator LOGIN PASSWORD 'operator' SUPERUSER\""
pg "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='operator'\"" | grep -q 1 || \
  pg "createdb -O operator operator"

echo "==> installing python deps"
pip install --quiet --upgrade pip
pip install --quiet -r services/voice/requirements.txt
pip install --quiet pytest pytest-asyncio ruff

echo "==> applying migrations and seed"
export PGPASSWORD=operator
DB="postgresql://operator:operator@127.0.0.1:5432/operator"
for f in db/migrations/*.sql db/seed/*.sql; do
  echo "    $f"
  psql "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> schema assertions"
psql "$DB" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 | sed 's/^.*NOTICE:  //'

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i 's#^DATABASE_URL=.*#DATABASE_URL=postgresql://operator:operator@127.0.0.1:5432/operator#' .env
  sed -i 's#^REDIS_URL=.*#REDIS_URL=redis://127.0.0.1:6379/0#' .env
fi

cat <<'MSG'

Ready.

  make test     schema assertions + python tests
  make api      voice service on :8000

Twilio webhook target, once port 8000 is public:
  https://$CODESPACE_NAME-8000.app.github.dev/twilio/voice

If Twilio reports 31920, the Codespaces relay is rejecting the WebSocket
upgrade. Not fixable from inside the container. Deploy to Fly instead.
See SETUP.md.

MSG
