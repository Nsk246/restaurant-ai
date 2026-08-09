DB ?= postgresql://operator:operator@127.0.0.1:5432/operator
# Tests get their own database. They call /api/demo/reset, which wipes the
# tenant, so pointing them at the development database destroys whatever you
# had on the rail.
TEST_DB ?= postgresql://operator:operator@127.0.0.1:5432/operator_test

.PHONY: up down migrate seed test test-db test-voice test-setup api portal portal-dev reset lint

up:       ; docker compose up -d
down:     ; docker compose down
migrate:  ; @bash db/migrate.sh "$(DB)"
seed:     ; @for f in db/seed/*.sql; do echo "-> $$f"; psql "$(DB)" -q -v ON_ERROR_STOP=1 -f $$f; done
test-db: test-setup
	@psql "$(TEST_DB)" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 | sed 's/^.*NOTICE:  //'
test-setup:
	@psql "$(DB)" -tAc "SELECT 1 FROM pg_database WHERE datname='operator_test'" \
	  | grep -q 1 || psql "$(DB)" -q -c "CREATE DATABASE operator_test OWNER operator"
	@bash db/migrate.sh "$(TEST_DB)" >/dev/null
	@for f in db/seed/*.sql; do psql "$(TEST_DB)" -q -v ON_ERROR_STOP=1 -f $$f; done
test-voice: test-setup
	@cd services/voice && TEST_DATABASE_URL="$(TEST_DB)" python -m pytest -q
test: test-db test-voice
lint:     ; ruff check services/voice
portal:   ; cd web/portal && npm install --silent && npm run build
portal-dev: ; cd web/portal && npm run dev
# --proxy-headers is what makes X-Forwarded-* visible. Without it Twilio
# signature validation fails behind Codespaces or any load balancer.
api:      ; cd services/voice && uvicorn app.main:app --host 0.0.0.0 --port 8000 \
	    --reload --proxy-headers --forwarded-allow-ips="*"
reset:    ; docker compose down -v && docker compose up -d && sleep 6 && $(MAKE) migrate seed test
