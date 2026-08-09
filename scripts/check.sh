#!/usr/bin/env bash
# Pre-package check. Everything that must be true before a zip goes out.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
say() { printf '%-52s %s\n' "$1" "$2"; }

# 1. No test may query a hardcoded generated code: new_item() suffixes names
#    per run, so a literal code is stale the moment it is written.
if grep -rn "WHERE code='" services/voice/tests/ 2>/dev/null; then
  say "tests query a literal code" "FAIL"; fail=1
else
  say "tests query no literal codes" "ok"
fi

# 2. The suite must never call a real model. Slow, costs money, and makes the
#    result depend on what the model felt like returning that day.
if grep -q 'os.environ\["GEMINI_API_KEY"\] = ""' services/voice/tests/conftest.py; then
  say "test suite pinned off the live model" "ok"
else
  say "test suite may call the live model" "FAIL"; fail=1
fi

# 3. Settings must resolve .env absolutely. A relative path silently loads
#    nothing when launched from services/voice.
if grep -q 'Path(__file__).resolve().parents\[3\]' services/voice/app/config.py; then
  say ".env resolved absolutely" "ok"
else
  say ".env path is relative" "FAIL"; fail=1
fi

echo
[ "$fail" -eq 0 ] && echo "all checks passed" || { echo "checks failed"; exit 1; }
