DB ?= postgresql://operator:operator@127.0.0.1:5432/operator

.PHONY: up down migrate seed test test-db test-voice api reset lint

up:       ; docker compose up -d
down:     ; docker compose down
migrate:  ; @bash db/migrate.sh "$(DB)"
seed:     ; @for f in db/seed/*.sql; do echo "-> $$f"; psql "$(DB)" -q -v ON_ERROR_STOP=1 -f $$f; done
test-db:  ; @psql "$(DB)" -q -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql 2>&1 | sed 's/^.*NOTICE:  //'
test-voice: ; @cd services/voice && python -m pytest -q
test: test-db test-voice
lint:     ; ruff check services/voice
api:      ; cd services/voice && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
reset:    ; docker compose down -v && docker compose up -d && sleep 6 && $(MAKE) migrate seed test
