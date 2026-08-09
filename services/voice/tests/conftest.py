"""Test environment.

Settings are read once and cached, so these must be set before anything
imports `app.config`. Putting them here rather than in a test module keeps
import order out of the tests themselves.
"""

import os
import warnings

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REALTIME_PROVIDER", "mock")
os.environ.setdefault("PUBLIC_BASE_URL", "https://demo.test")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")
# The app opens its own pool through its lifespan, on its own event loop.
# Sharing one pool across loops raises InterfaceError in ways that look like
# a database problem and are not.
# Default to a dedicated test database, never the development one. The API
# tests call /api/demo/reset, which wipes the tenant: pointing them at the
# development database silently destroys whatever was on the rail.
_TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://operator:operator@127.0.0.1:5432/operator_test"
)
os.environ["TEST_DATABASE_URL"] = _TEST_DSN
os.environ["DATABASE_URL"] = _TEST_DSN

warnings.filterwarnings("ignore", category=DeprecationWarning)


# Tests share the development database, so they must not leave rows behind.
# Without this, `make test` fills the portal's recent-calls list with
# +1615555xxxx entries that have no outcome, which looks like a broken product
# rather than test residue.
TEST_EXTERNAL_PREFIXES = ("test-", "api-", "open-", "live-", "other-", "stale-")


def pytest_sessionfinish(session, exitstatus):
    import asyncio
    import os

    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return

    async def cleanup():
        try:
            import asyncpg

            conn = await asyncpg.connect(dsn, timeout=3)
        except Exception:
            return
        try:
            patterns = [f"{p}%" for p in TEST_EXTERNAL_PREFIXES]
            # Orders cascade from the conversation only via SET NULL, so drop
            # them explicitly before the conversations they belong to.
            await conn.execute(
                """
                DELETE FROM orders WHERE conversation_id IN (
                    SELECT id FROM conversations WHERE external_id LIKE ANY($1::text[])
                )
                """,
                patterns,
            )
            await conn.execute(
                "DELETE FROM conversations WHERE external_id LIKE ANY($1::text[])",
                patterns,
            )
            await conn.execute(
                "DELETE FROM restaurants WHERE slug = 'test-diner'"
            )
        finally:
            await conn.close()

    try:
        asyncio.run(cleanup())
    except Exception as exc:
        # Belt and braces on top of the dedicated test database. If it fails,
        # say so rather than hiding it, but never fail the run over cleanup.
        print(f"test cleanup skipped: {type(exc).__name__}: {exc}")
