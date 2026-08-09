#!/usr/bin/env bash
# Create the GitHub repo and push. Run once, from the project root.
set -euo pipefail
REPO="${1:-restaurant-ai}"

git init -b main
cat > .gitignore <<'GIT'
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.env
node_modules/
dist/
.venv/
GIT

git add -A
git commit -m "M0 data foundation, M1 media bridge

Schema with an enforced order state machine, idempotent seed, and 13 assertions.
Twilio media bridge with barge-in, mu-law codec without audioop, provider
adapter interface, and 26 tests covering the paths that break voice agents."

gh repo create "$REPO" --private --source=. --remote=origin --push
gh api "repos/{owner}/$REPO/branches/main/protection" -X PUT \
  -F required_status_checks='{"strict":true,"contexts":["schema","voice"]}' \
  -F enforce_admins=false -F required_pull_request_reviews='' \
  -F restrictions='' 2>/dev/null || echo "note: branch protection needs a paid plan on private repos"

echo "Repo ready. Open in Codespaces from the GitHub UI."
