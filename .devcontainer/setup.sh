#!/usr/bin/env bash
set -euo pipefail

pip install --quiet -r services/voice/requirements.txt
pip install --quiet pytest pytest-asyncio ruff

until pg_isready -h db -U operator >/dev/null 2>&1; do sleep 1; done

export DB="postgresql://operator:operator@db:5432/operator"
for f in db/migrations/*.sql db/seed/*.sql; do
  echo "applying $f"
  psql "$DB" -q -v ON_ERROR_STOP=1 -f "$f"
done
psql "$DB" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 | sed 's/^.*NOTICE:  //'

[ -f .env ] || cp .env.example .env

cat <<'MSG'

Ready.

  make api      start the voice service on :8000
  make test     schema assertions + python tests

Twilio webhook target, once port 8000 is public:
  https://$CODESPACE_NAME-8000.app.github.dev/twilio/voice

If Twilio reports error 31920, the Codespaces relay is rejecting the
unauthenticated WebSocket upgrade. That is a known failure and it is not
something you can fix from inside the container. Deploy to Fly and point
Twilio there instead. See docs/PLAN.md.

MSG
