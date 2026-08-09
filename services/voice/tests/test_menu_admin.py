"""Menu management.

The menu used to be seed SQL, so a price change was a deploy. These cover the
door a restaurant actually uses, and the one a real menu gets imported through.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.agent import menu as menu_mod
from app.main import app

DSN = os.environ["TEST_DATABASE_URL"]

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def admin():
    try:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, timeout=3)
    except Exception as exc:
        pytest.skip(f"no database at {DSN}: {type(exc).__name__}: {exc}")
    with TestClient(app) as client:
        client.post("/api/demo/reset")
        yield {"client": client, "pool": pool}
    await pool.close()


async def snapshot_names(pool):
    async with pool.acquire() as conn:
        rid = await conn.fetchval("SELECT id FROM restaurants LIMIT 1")
        snap = await menu_mod.snapshot(conn, rid)
    return [i["name"] for cat in snap for i in cat["items"]]


# Menu items deliberately survive /api/demo/reset, so a prospect's menu is
# not wiped between demos. That means tests must not reuse dish names across
# runs or the second run collides on the unique-name constraint.
RUN = uuid.uuid4().hex[:6]


def new_item(client, name, category="Specials", price=12.5, **kw):
    body = {"name": f"{name} {RUN}", "category": category, "price": price, **kw}
    return client.post("/api/menu/items", json=body)


# ------------------------------------------------------------------ creating


async def test_a_new_item_reaches_the_agent_menu(admin):
    r = new_item(admin["client"], "Catfish Po Boy", price=14.0)
    assert r.status_code == 200, r.text
    assert r.json()["code"].startswith("catfish-po-boy")
    assert r.json()["name"] in await snapshot_names(admin["pool"])


async def test_a_new_category_is_created_on_demand(admin):
    """A restaurant adding a special should not have to create the section."""
    new_item(admin["client"], "Peach Cobbler", category="Tonight Only", price=9.0)
    async with admin["pool"].acquire() as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM menu_categories WHERE name='Tonight Only'"
        )


async def test_price_is_stored_in_cents_not_floats(admin):
    code = new_item(admin["client"], "Odd Price", price=10.07).json()["code"]
    async with admin["pool"].acquire() as conn:
        cents = await conn.fetchval(
            "SELECT price_cents FROM menu_items WHERE code=$1", code
        )
    assert cents == 1007 and isinstance(cents, int)


async def test_duplicate_names_get_distinct_codes(admin):
    """Two dishes can legitimately slugify the same. That must not error."""
    a = new_item(admin["client"], "House Salad").json()["code"]
    b = new_item(admin["client"], "House  Salad!").json()["code"]
    assert a != b


async def test_a_created_item_is_orderable_by_its_code(admin):
    """The whole point: the agent can sell what was just added."""
    from app.agent import orders
    from app.agent import session as session_mod
    from app.agent.tools import ToolDispatcher

    code = new_item(admin["client"], "Fried Okra", price=6.0).json()["code"]
    pool = admin["pool"]
    async with pool.acquire() as conn:
        number = await conn.fetchval("SELECT e164 FROM phone_numbers LIMIT 1")
        tenant = await menu_mod.resolve_tenant(conn, number)
        snap = await menu_mod.snapshot(conn, tenant.id)
        conv = await session_mod.open_conversation(
            conn, restaurant_id=tenant.id, external_id=f"test-{uuid.uuid4()}"
        )
    d = ToolDispatcher(pool, tenant=tenant, menu=snap, conversation_id=conv)
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch("add_item", {"item_code": code})
    assert "error" not in r, r
    async with pool.acquire() as conn:
        q = await orders.quote(conn, order_id=d.order_id)
    assert q.lines[0].name == f"Fried Okra {RUN}"
    assert q.subtotal_cents == 600


# ------------------------------------------------------------------ editing


async def test_price_change_takes_effect_without_a_deploy(admin):
    c = admin["client"]
    code = new_item(c, "Hush Puppies", price=5.0).json()["code"]
    r = c.patch(f"/api/menu/items/{code}", json={"price": 6.5})
    assert r.status_code == 200 and r.json()["price"] == 6.5


async def test_renaming_keeps_the_code_stable(admin):
    """The code is what the agent emits in a tool call. Reassigning it on a
    rename would break a call already in progress."""
    c = admin["client"]
    code = new_item(c, "Chicken Plate").json()["code"]
    r = c.patch(f"/api/menu/items/{code}", json={"name": f"Chicken Dinner {RUN}"})
    assert r.json()["code"] == code, "renaming must not reassign the code"
    assert r.json()["name"] == f"Chicken Dinner {RUN}"


async def test_only_the_fields_sent_are_changed(admin):
    c = admin["client"]
    code = new_item(c, "Brisket", price=22.0, description="Twelve hours").json()["code"]
    c.patch(f"/api/menu/items/{code}", json={"price": 24.0})
    async with admin["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "SELECT description, price_cents FROM menu_items WHERE code=$1", code
        )
    assert row["description"] == "Twelve hours" and row["price_cents"] == 2400


async def test_editing_an_unknown_item_is_a_404(admin):
    r = admin["client"].patch("/api/menu/items/nope", json={"price": 1.0})
    assert r.status_code == 404


async def test_an_empty_patch_is_rejected(admin):
    code = new_item(admin["client"], "Grits").json()["code"]
    assert admin["client"].patch(f"/api/menu/items/{code}", json={}).status_code == 400


async def test_deactivating_removes_it_from_the_agent_menu(admin):
    c = admin["client"]
    code = new_item(c, "Seasonal Soup").json()["code"]
    c.patch(f"/api/menu/items/{code}", json={"is_active": False})
    assert f"Seasonal Soup {RUN}" not in await snapshot_names(admin["pool"])


# ----------------------------------------------------------------- deleting


async def test_an_unused_item_is_deleted_outright(admin):
    c = admin["client"]
    code = new_item(c, "Typo Dish").json()["code"]
    r = c.delete(f"/api/menu/items/{code}")
    assert r.json()["deleted"] is True


async def test_an_item_with_orders_is_deactivated_not_deleted(admin):
    """Hard-deleting would orphan past order lines, and those are what the
    restaurant's takings are reconciled against."""
    from app.agent import session as session_mod
    from app.agent.tools import ToolDispatcher

    c = admin["client"]
    code = new_item(c, "Sold Item", price=8.0).json()["code"]
    pool = admin["pool"]
    async with pool.acquire() as conn:
        number = await conn.fetchval("SELECT e164 FROM phone_numbers LIMIT 1")
        tenant = await menu_mod.resolve_tenant(conn, number)
        snap = await menu_mod.snapshot(conn, tenant.id)
        conv = await session_mod.open_conversation(
            conn, restaurant_id=tenant.id, external_id=f"test-{uuid.uuid4()}"
        )
    d = ToolDispatcher(pool, tenant=tenant, menu=snap, conversation_id=conv)
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": code})
    await d.dispatch("review_order", {})
    await d.dispatch("confirm_order", {})

    r = c.delete(f"/api/menu/items/{code}")
    assert r.json()["deactivated"] is True
    assert r.json()["orders"] >= 1

    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM menu_items WHERE code=$1", code
        ), "the row must survive so past orders still resolve"


async def test_deleting_an_unknown_item_is_a_404(admin):
    assert admin["client"].delete("/api/menu/items/nope").status_code == 404


# ---------------------------------------------------------------- modifiers


async def test_a_modifier_group_can_be_attached_to_a_new_item(admin):
    c = admin["client"]
    code = new_item(c, "Wing Basket").json()["code"]
    r = c.post(f"/api/menu/items/{code}/modifiers", params={"group": "Heat Level"})
    assert r.status_code == 200

    async with admin["pool"].acquire() as conn:
        rid = await conn.fetchval("SELECT id FROM restaurants LIMIT 1")
        snap = await menu_mod.snapshot(conn, rid)
    item = next(
        i for cat in snap for i in cat["items"] if i["code"] == code
    )
    assert item["modifier_groups"][0]["name"] == "Heat Level"


async def test_attaching_an_unknown_group_is_a_404(admin):
    c = admin["client"]
    code = new_item(c, "Plain Thing").json()["code"]
    r = c.post(f"/api/menu/items/{code}/modifiers", params={"group": "Nope"})
    assert r.status_code == 404


async def test_a_duplicate_dish_name_is_a_conflict_not_a_crash(admin):
    """The UI needs to explain this, so it cannot arrive as a 500."""
    c = admin["client"]
    name = f"Twice Named {uuid.uuid4().hex[:6]}"
    assert c.post(
        "/api/menu/items", json={"name": name, "category": "X", "price": 5.0}
    ).status_code == 200
    assert c.post(
        "/api/menu/items", json={"name": name, "category": "X", "price": 5.0}
    ).status_code == 409


async def test_renaming_onto_an_existing_name_is_a_conflict(admin):
    c = admin["client"]
    a = f"First {uuid.uuid4().hex[:6]}"
    b = f"Second {uuid.uuid4().hex[:6]}"
    c.post("/api/menu/items", json={"name": a, "category": "X", "price": 5.0})
    code = c.post(
        "/api/menu/items", json={"name": b, "category": "X", "price": 5.0}
    ).json()["code"]
    assert c.patch(f"/api/menu/items/{code}", json={"name": a}).status_code == 409


# ------------------------------------------------------------------ import


async def test_pasted_menu_is_structured_into_items(admin):
    text = """STARTERS
Fried Green Tomatoes  9
Pimento Cheese Dip 11.50

MAINS
Nashville Hot Chicken .... 18.50
Smash Burger - 16
Ribeye 38

DRINKS
Sweet Tea 3.5"""
    r = admin["client"].post("/api/menu/import/preview", json={"text": text})
    assert r.status_code == 200
    names = [i["name"] for i in r.json()["items"]]
    assert "Nashville Hot Chicken" in names
    assert "Ribeye" in names
    cats = {i["category"] for i in r.json()["items"]}
    assert "MAINS" in cats or "Mains" in cats


async def test_preview_writes_nothing(admin):
    """Parsing menus is inexact. Nobody should find out an import was mangled
    by hearing the agent offer it to a customer."""
    before = len(await snapshot_names(admin["pool"]))
    admin["client"].post(
        "/api/menu/import/preview", json={"text": "SPECIALS\nGhost Dish 99"}
    )
    assert len(await snapshot_names(admin["pool"])) == before


async def test_modifier_lines_are_not_imported_as_dishes(admin):
    """'add bacon +2.50' is an extra, not a plate."""
    text = "MAINS\nSmash Burger 16\nadd bacon +2.50\nextra cheese + 1.50"
    r = admin["client"].post("/api/menu/import/preview", json={"text": text})
    names = [i["name"].lower() for i in r.json()["items"]]
    assert not any("bacon" in n or "cheese" in n for n in names), names


async def test_commit_creates_the_reviewed_items(admin):
    tag = uuid.uuid4().hex[:6]
    items = [
        {"name": f"Imported Gumbo {tag}", "category": "Imported", "price": 13.0},
        {"name": f"Imported Boudin {tag}", "category": "Imported", "price": 8.5},
    ]
    r = admin["client"].post("/api/menu/import/commit", json={"items": items})
    assert r.json()["created"] == 2
    names = await snapshot_names(admin["pool"])
    assert f"Imported Gumbo {tag}" in names


async def test_items_without_a_price_are_skipped_and_reported(admin):
    """Guessing a price is worse than not importing the item."""
    tag = uuid.uuid4().hex[:6]
    items = [
        {"name": f"Priced {tag}", "category": "Imported", "price": 10.0},
        {"name": f"Unpriced {tag}", "category": "Imported", "price": None},
    ]
    r = admin["client"].post("/api/menu/import/commit", json={"items": items}).json()
    assert r["created"] == 1
    assert any("Unpriced" in n for n in r["skipped_no_price"])


async def test_replace_deactivates_the_existing_menu(admin):
    """Onboarding a real restaurant over the sample data.

    Restores the seed menu afterwards. `replace` deactivates every item, and
    the seed is idempotent on name, so re-seeding does not turn them back on:
    leaving this test's effect in place breaks every later run.
    """
    tag = uuid.uuid4().hex[:6]
    pool = admin["pool"]
    assert "Smash Burger" in await snapshot_names(pool)

    async with pool.acquire() as conn:
        before = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM menu_items WHERE is_active"
            )
        ]
    try:
        admin["client"].post(
            "/api/menu/import/commit",
            json={
                "items": [
                    {"name": f"Only Dish {tag}", "category": "New", "price": 10.0}
                ],
                "replace": True,
            },
        )
        names = await snapshot_names(pool)
        assert "Smash Burger" not in names
        assert f"Only Dish {tag}" in names

        # Deactivated, not deleted: last week's takings are reconciled
        # against those line items.
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT 1 FROM menu_items WHERE name='Smash Burger'"
            ), "the row must survive a replace"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE menu_items SET is_active=true WHERE id = ANY($1::uuid[])",
                before,
            )
