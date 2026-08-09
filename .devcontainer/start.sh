#!/usr/bin/env bash
# Runs on every container start, including after a stop/resume.
# Postgres and Redis do not survive a container restart on their own.
set -euo pipefail

sudo pg_ctlcluster 16 main start 2>/dev/null || true
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
