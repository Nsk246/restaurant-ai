"""Portal and kitchen API tests, against a real database."""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.agent import menu as menu_mod
from app.agent import session as session_mod
from app.agent.tools import ToolDispatcher
from app.main import app

DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://operator:operator@127.0.0.1:5432/operator"
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api():
    """Two pools on purpose.

    The app opens its own inside its lifespan, on the thread TestClient runs
    it on. The test gets a separate one for direct database work. Sharing a
    single asyncpg pool across two event loops raises InterfaceError, which
    reads like a database outage and is not one.
    """
    try:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, timeout=3)
    except Exception as exc:
        pytest.skip(f"no database at {DSN}: {type(exc).__name__}: {exc}")
    with TestClient(app) as client:
        client.post("/api/demo/reset")
        yield {"client": client, "pool": pool}
    await pool.close()


async def _fire_one(pool) -> int:
    """Put a real ticket on the rail through the same path a call uses."""
    async with pool.acquire() as conn:
        number = await conn.fetchval(
            "SELECT e164 FROM phone_numbers WHERE is_active ORDER BY created_at LIMIT 1"
        )
        tenant = await menu_mod.resolve_tenant(conn, number)
        snap = await menu_mod.snapshot(conn, tenant.id)
        conv = await session_mod.open_conversation(
            conn,
            restaurant_id=tenant.id,
            channel="phone",
            external_id=f"api-{uuid.uuid4()}",
            from_e164="+16155559999",
        )
    d = ToolDispatcher(pool, tenant=tenant, menu=snap, conversation_id=conv)
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": "fries", "quantity": 2})
    await d.dispatch("review_order", {})
    out = await d.dispatch("confirm_order", {"customer_name": "Rae"})
    return out["order_number"]


async def test_restaurant_endpoint_returns_the_dialled_number(api):
    body = api["client"].get("/api/restaurant").json()
    assert body["name"] == "Broadway Kitchen"
    assert body["phone"].startswith("+")


async def test_menu_includes_unavailable_items_for_the_86_toggle(api):
    body = api["client"].get("/api/menu").json()
    codes = [i["code"] for c in body["categories"] for i in c["items"]]
    assert "smash-burger" in codes
    assert all("available" in i for c in body["categories"] for i in c["items"])


async def test_86_toggle_removes_the_item_from_the_agent_menu(api):
    c = api["client"]
    assert c.post("/api/menu/fries/availability", params={"available": False}).json()[
        "available"
    ] is False
    async with api["pool"].acquire() as conn:
        rid = await conn.fetchval("SELECT id FROM restaurants LIMIT 1")
        snap = await menu_mod.snapshot(conn, rid)
    names = [i["name"] for cat in snap for i in cat["items"]]
    assert "Fries" not in names
    c.post("/api/menu/fries/availability", params={"available": True})


async def test_86_toggle_on_an_unknown_item_is_a_404(api):
    assert api["client"].post(
        "/api/menu/truffle-risotto/availability", params={"available": False}
    ).status_code == 404


async def test_fired_order_appears_on_the_rail(api):
    number = await _fire_one(api["pool"])
    rail = api["client"].get("/api/rail").json()
    ticket = next(t for t in rail["tickets"] if t["number"] == number)
    assert ticket["status"] == "fired"
    assert ticket["lines"][0]["quantity"] == 2
    assert ticket["age_seconds"] >= 0


async def test_rail_is_ordered_oldest_first(api):
    await _fire_one(api["pool"])
    await _fire_one(api["pool"])
    tickets = api["client"].get("/api/rail").json()["tickets"]
    ages = [t["age_seconds"] for t in tickets]
    assert ages == sorted(ages, reverse=True), "oldest ticket must be first"


async def test_ticket_advances_along_the_rail(api):
    number = await _fire_one(api["pool"])
    rail = api["client"].get("/api/rail").json()
    oid = next(t["id"] for t in rail["tickets"] if t["number"] == number)
    c = api["client"]
    assert c.post(f"/api/rail/{oid}/advance", params={"to": "preparing"}).json()[
        "status"
    ] == "preparing"
    assert c.post(f"/api/rail/{oid}/advance", params={"to": "ready"}).json()[
        "status"
    ] == "ready"


async def test_illegal_advance_is_refused_not_corrupted(api):
    """A double tap on a busy kitchen screen must not corrupt an order."""
    number = await _fire_one(api["pool"])
    rail = api["client"].get("/api/rail").json()
    oid = next(t["id"] for t in rail["tickets"] if t["number"] == number)
    c = api["client"]
    c.post(f"/api/rail/{oid}/advance", params={"to": "ready"})
    c.post(f"/api/rail/{oid}/advance", params={"to": "completed"})
    # completed is terminal; going back must fail rather than silently work
    assert (
        c.post(f"/api/rail/{oid}/advance", params={"to": "preparing"}).status_code == 409
    )


async def test_completed_ticket_leaves_the_rail(api):
    number = await _fire_one(api["pool"])
    c = api["client"]
    oid = next(
        t["id"] for t in c.get("/api/rail").json()["tickets"] if t["number"] == number
    )
    c.post(f"/api/rail/{oid}/advance", params={"to": "ready"})
    c.post(f"/api/rail/{oid}/advance", params={"to": "completed"})
    assert all(t["number"] != number for t in c.get("/api/rail").json()["tickets"])


async def test_advance_to_a_nonsense_status_is_rejected(api):
    number = await _fire_one(api["pool"])
    c = api["client"]
    oid = next(
        t["id"] for t in c.get("/api/rail").json()["tickets"] if t["number"] == number
    )
    assert c.post(f"/api/rail/{oid}/advance", params={"to": "on_fire"}).status_code == 400


async def test_calls_endpoint_lists_conversations(api):
    await _fire_one(api["pool"])
    calls = api["client"].get("/api/calls").json()["calls"]
    assert calls and "started_at" in calls[0]


async def test_demo_reset_clears_the_rail_and_unblocks_86d_items(api):
    c = api["client"]
    await _fire_one(api["pool"])
    c.post("/api/menu/fries/availability", params={"available": False})
    assert c.post("/api/demo/reset").json()["reset"] is True
    assert c.get("/api/rail").json()["tickets"] == []
    menu = c.get("/api/menu").json()
    fries = next(
        i for cat in menu["categories"] for i in cat["items"] if i["code"] == "fries"
    )
    assert fries["available"] is True


async def test_demo_reset_keeps_the_menu_itself(api):
    """A prospect's own menu must survive a reset between demos."""
    c = api["client"]
    before = len(
        [i for cat in c.get("/api/menu").json()["categories"] for i in cat["items"]]
    )
    c.post("/api/demo/reset")
    after = len(
        [i for cat in c.get("/api/menu").json()["categories"] for i in cat["items"]]
    )
    assert before == after
