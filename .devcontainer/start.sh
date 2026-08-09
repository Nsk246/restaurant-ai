#!/usr/bin/env bash
# Runs on every container start, including after a stop/resume.
# Postgres and Redis do not survive a container restart on their own.
set -euo pipefail

# Do not hardcode the major version: the base image decides which Postgres
# lands, and a wrong number fails silently and leaves you with no database.
PGVER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -1)"
if [ -n "$PGVER" ]; then
  sudo pg_ctlcluster "$PGVER" main start 2>/dev/null || true
else
  sudo service postgresql start >/dev/null 2>&1 || true
fi
sudo service redis-server start >/dev/null 2>&1 || true

for i in $(seq 1 30); do
  if pg_isready -q -h 127.0.0.1; then exit 0; fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: postgres did not start within 30s." >&2
    echo "Try: sudo pg_ctlcluster 16 main start" >&2
    exit 1
  fi
  sleep 1
done
