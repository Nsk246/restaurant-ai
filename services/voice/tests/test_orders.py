"""Order accuracy suite. This is the M2 gate.

Runs against a real Postgres with the real seed, through the same dispatcher
the model calls. Skipped automatically if no database is reachable, so the
unit suite still runs anywhere.

The scenarios are the ones that actually go wrong on the phone: modifiers,
mid-order changes, 86'd items, required choices the caller skipped, repeated
confirmations, and callers who say yes before hearing the order back.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.agent import menu as menu_mod
from app.agent import orders
from app.agent import session as session_mod
from app.agent.tools import ToolDispatcher
from app.kitchen import InternalKDS

DSN = os.environ["TEST_DATABASE_URL"]
# Look the tenant up by slug, not by phone number. The seeded number is meant
# to be replaced with a real Twilio number, and a test that hardcodes it
# breaks the moment someone does the thing they were told to do.
PILOT_SLUG = "pilot"

pytestmark = pytest.mark.asyncio


async def _pool():
    """Connect, trying TCP then the unix socket.

    A Debian Postgres may be listening only on the socket, in which case psql
    works fine while a TCP connection is refused. Trying both stops that
    difference from silently disabling this whole suite.
    """
    attempts = [DSN]
    if "127.0.0.1" in DSN or "localhost" in DSN:
        attempts.append("postgresql://operator:operator@/operator?host=/var/run/postgresql")
    errors = []
    for dsn in attempts:
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=3)
        except Exception as exc:
            errors.append(f"{dsn.split('@')[-1]}: {type(exc).__name__}: {exc}")
    # Say why. A skip with no reason is how a suite quietly stops running and
    # everyone keeps trusting it.
    pytest.skip("no database reachable -> " + " | ".join(errors))


@pytest.fixture
async def kit():
    pool = await _pool()
    async with pool.acquire() as conn:
        number = await conn.fetchval(
            """
            SELECT p.e164 FROM phone_numbers p
            JOIN restaurants r ON r.id = p.restaurant_id
            WHERE r.slug = $1 AND p.is_active
            ORDER BY p.created_at LIMIT 1
            """,
            PILOT_SLUG,
        )
        if number is None:
            pytest.skip(f"no active phone number seeded for slug {PILOT_SLUG!r}")
        tenant = await menu_mod.resolve_tenant(conn, number)
        if tenant is None:
            pytest.skip("pilot tenant not seeded")
        snap = await menu_mod.snapshot(conn, tenant.id)
    fired: list[dict] = []

    class Sink(InternalKDS):
        async def fire(self, order_id, ticket):
            fired.append(ticket)

    async def make(conv: str | None = None):
        # Orders reference a conversation, exactly as they do on a real call.
        async with pool.acquire() as c:
            conv_id = await session_mod.open_conversation(
                c,
                restaurant_id=tenant.id,
                channel="phone",
                external_id=conv or f"test-{uuid.uuid4()}",
                from_e164="+16155559999",
            )
        return ToolDispatcher(
            pool, tenant=tenant, menu=snap, conversation_id=conv_id, kitchen=Sink()
        )

    yield {
        "make": make,
        "menu": snap,
        "tenant": tenant,
        "pool": pool,
        "fired": fired,
        "number": number,
    }
    await pool.close()


def item_id(menu, name):
    """Short code for an item. Named for what the tools take, not the PK."""
    for cat in menu:
        for it in cat["items"]:
            if it["name"] == name:
                return it["code"]
    raise AssertionError(f"{name} not in snapshot")


def modifier_id(menu, item_name, mod_name):
    for cat in menu:
        for it in cat["items"]:
            if it["name"] == item_name:
                for g in it.get("modifier_groups", []):
                    for o in g["options"]:
                        if o["name"] == mod_name:
                            return o["code"]
    raise AssertionError(f"{mod_name} not a modifier of {item_name}")


# --------------------------------------------------------------- the basics


async def test_tenant_resolves_from_the_dialled_number(kit):
    """Whatever number is seeded must route to the pilot restaurant.

    Asserted against the seeded value rather than a literal, so replacing the
    placeholder with a real Twilio number does not break the suite.
    """
    assert kit["tenant"].name == "Broadway Kitchen"
    assert kit["number"].startswith("+")


async def test_an_unknown_number_resolves_to_no_tenant(kit):
    """A call to a number we do not own must be refused, not served."""
    async with kit["pool"].acquire() as conn:
        assert await menu_mod.resolve_tenant(conn, "+19999999999") is None


async def test_snapshot_contains_ids_prices_and_aliases(kit):
    burger = None
    for cat in kit["menu"]:
        for it in cat["items"]:
            if it["name"] == "Smash Burger":
                burger = it
    assert burger and burger["price"] == 16.50
    assert "burger" in burger["aliases"]
    # Short, stable, and speakable in a log. Not a uuid.
    assert burger["code"] == "smash-burger"


async def test_simple_order_reaches_the_kitchen(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item", {"item_code": item_id(kit["menu"], "Smash Burger"), "quantity": 2}
    )
    assert "error" not in r
    await d.dispatch("review_order", {})
    out = await d.dispatch("confirm_order", {"customer_name": "Sam"})
    assert out["order_number"] >= 1
    assert kit["fired"], "nothing reached the kitchen"
    assert kit["fired"][-1]["lines"][0]["quantity"] == 2


async def test_totals_include_modifiers_and_tax(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Smash Burger"),
            "quantity": 1,
            "modifier_codes": [modifier_id(kit["menu"], "Smash Burger", "Bacon")],
        },
    )
    rev = await d.dispatch("review_order", {})
    # 16.50 + 2.50 bacon = 19.00, tax 9.25% floor = 1.75, total 20.75
    assert rev["total"] == pytest.approx(20.75, abs=0.01)


async def test_note_is_carried_to_the_kitchen_verbatim(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Smash Burger"),
            "note": "no pickles, extra sauce on the side",
        },
    )
    await d.dispatch("review_order", {})
    await d.dispatch("confirm_order", {})
    assert kit["fired"][-1]["lines"][0]["note"] == "no pickles, extra sauce on the side"


# ------------------------------------------------- the ways callers actually talk


async def test_caller_changes_quantity_mid_order(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item", {"item_code": item_id(kit["menu"], "Fries"), "quantity": 1}
    )
    await d.dispatch("change_quantity", {"line_id": r["line_id"], "quantity": 3})
    rev = await d.dispatch("review_order", {})
    assert rev["lines"][0]["quantity"] == 3


async def test_caller_removes_an_item(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    a = await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Fries")})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Sweet Tea"),
            "modifier_codes": [modifier_id(kit["menu"], "Sweet Tea", "Large")],
        },
    )
    await d.dispatch("change_quantity", {"line_id": a["line_id"], "quantity": 0})
    rev = await d.dispatch("review_order", {})
    assert len(rev["lines"]) == 1


async def test_find_item_matches_what_people_say_out_loud(kit):
    d = await kit["make"]()
    r = await d.dispatch("find_item", {"query": "the wings"})
    assert r["candidates"] and r["candidates"][0]["name"] == "Nashville Hot Wings"
    assert r["candidates"][0]["code"] == "nashville-hot-wings"


async def test_find_item_returns_nothing_for_a_dish_we_do_not_have(kit):
    d = await kit["make"]()
    r = await d.dispatch("find_item", {"query": "pad thai"})
    assert r["candidates"] == []


async def test_spoken_summary_is_readable_aloud(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Nashville Hot Chicken"),
            "quantity": 2,
            "modifier_codes": [modifier_id(kit["menu"], "Nashville Hot Chicken", "Hot")],
        },
    )
    rev = await d.dispatch("review_order", {})
    s = rev["spoken_summary"]
    assert "2 Nashville Hot Chicken" in s and "Hot" in s and "Total" in s


# ------------------------------------------------------------- the guardrails


async def test_model_cannot_invent_a_menu_item(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch("add_item", {"item_code": "truffle-risotto"})
    assert "error" in r and "not on the menu" in r["error"]


async def test_cannot_sell_an_86d_item(kit):
    pool = kit["pool"]
    iid = item_id(kit["menu"], "Fries")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE menu_items SET is_available=false WHERE code=$1", iid)
    try:
        d = await kit["make"]()
        await d.dispatch("start_order", {"order_type": "pickup"})
        r = await d.dispatch("add_item", {"item_code": iid})
        assert "error" in r and "out of" in r["error"]
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE menu_items SET is_available=true WHERE code=$1", iid
            )


async def test_86d_item_disappears_from_the_snapshot_entirely(kit):
    pool = kit["pool"]
    iid = item_id(kit["menu"], "Pecan Pie")
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "UPDATE menu_items SET is_available=false WHERE code=$1", iid
            )
            snap = await menu_mod.snapshot(conn, kit["tenant"].id)
        finally:
            await conn.execute(
                "UPDATE menu_items SET is_available=true WHERE code=$1", iid
            )
    names = [it["name"] for cat in snap for it in cat["items"]]
    assert "Pecan Pie" not in names


async def test_required_choice_must_be_made(kit):
    """Hot chicken needs a heat level. Skipping it prompts rather than guesses."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item", {"item_code": item_id(kit["menu"], "Nashville Hot Chicken")}
    )
    # The error is the group's own prompt, so it can be spoken verbatim.
    assert "error" in r and "hot" in r["error"].lower()


async def test_modifier_from_another_item_is_rejected(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Smash Burger"),
            "modifier_codes": [modifier_id(kit["menu"], "Ribeye", "Medium rare")],
        },
    )
    assert "error" in r


async def test_too_many_choices_in_a_single_select_group_rejected(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Nashville Hot Chicken"),
            "modifier_codes": [
                modifier_id(kit["menu"], "Nashville Hot Chicken", "Mild"),
                modifier_id(kit["menu"], "Nashville Hot Chicken", "Hot"),
            ],
        },
    )
    assert "error" in r and "at most" in r["error"]


async def test_confirm_without_reading_back_is_refused(kit):
    """The readback is not a prompt instruction. It is enforced."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Fries")})
    r = await d.dispatch("confirm_order", {})
    assert "error" in r and "read the order back" in r["error"]


async def test_adding_an_item_invalidates_a_previous_readback(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Fries")})
    await d.dispatch("review_order", {})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Cornbread")})
    r = await d.dispatch("confirm_order", {})
    assert "error" in r, "order changed after readback but confirmed anyway"


async def test_empty_order_cannot_be_confirmed(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("review_order", {})
    r = await d.dispatch("confirm_order", {})
    assert "error" in r


async def test_confirming_twice_does_not_fire_two_tickets(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Cornbread")})
    await d.dispatch("review_order", {})
    before = len(kit["fired"])
    first = await d.dispatch("confirm_order", {})
    second = await d.dispatch("confirm_order", {})
    assert first["order_number"] == second["order_number"]
    assert len(kit["fired"]) == before + 1, "double-fired to the kitchen"


async def test_start_order_twice_reuses_the_same_draft(kit):
    d = await kit["make"]()
    a = await d.dispatch("start_order", {"order_type": "pickup"})
    b = await d.dispatch("start_order", {"order_type": "pickup"})
    assert a["order_id"] == b["order_id"]


async def test_dine_in_requires_a_table(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "dine_in"})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Fries")})
    await d.dispatch("review_order", {})
    r = await d.dispatch("confirm_order", {})
    assert "error" in r, "dine-in confirmed with no table"


async def test_dine_in_succeeds_with_a_table(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "dine_in"})
    await d.dispatch("add_item", {"item_code": item_id(kit["menu"], "Fries")})
    await d.dispatch("review_order", {})
    r = await d.dispatch("confirm_order", {"table_label": "12"})
    assert r.get("order_number")


async def test_transfer_returns_the_restaurants_real_number(kit):
    d = await kit["make"]()
    r = await d.dispatch("transfer_to_human", {"reason": "caller asked"})
    assert r["transferring"]
    assert r["to"] == kit["tenant"].transfer_phone
    assert r["to"] != kit["number"], "transfer target must not be our own number"


async def test_unknown_tool_is_an_error_not_a_crash(kit):
    d = await kit["make"]()
    assert "error" in await d.dispatch("order_a_pizza", {})


async def test_menu_edit_does_not_rewrite_a_fired_order(kit):
    """Prices are snapshotted on the line. Tonight's order stays tonight's price."""
    pool = kit["pool"]
    iid = item_id(kit["menu"], "Cornbread")
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": iid})
    await d.dispatch("review_order", {})
    out = await d.dispatch("confirm_order", {})
    async with pool.acquire() as conn:
        # Capture the real price rather than hardcoding it. A failed run that
        # restores the wrong value silently poisons every later run.
        original = await conn.fetchval(
            "SELECT price_cents FROM menu_items WHERE code=$1", iid
        )
        try:
            await conn.execute(
                "UPDATE menu_items SET price_cents=9999 WHERE code=$1", iid
            )
            total = await conn.fetchval(
                "SELECT total_cents FROM orders WHERE order_number=$1"
                " AND business_date=CURRENT_DATE",
                out["order_number"],
            )
        finally:
            await conn.execute(
                "UPDATE menu_items SET price_cents=$2 WHERE code=$1", iid, original
            )
    assert total < 1000, "a later menu edit rewrote a fired order"


async def test_order_lands_in_the_database_exactly_as_spoken(kit):
    """The end-to-end check: what the caller said is what the kitchen gets."""
    pool = kit["pool"]
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Nashville Hot Chicken"),
            "quantity": 2,
            "modifier_codes": [
                modifier_id(kit["menu"], "Nashville Hot Chicken", "Extra Hot")
            ],
            "note": "no pickles",
        },
    )
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Mac and Cheese"),
            "quantity": 1,
        },
    )
    await d.dispatch("review_order", {})
    out = await d.dispatch("confirm_order", {"customer_name": "Priya"})

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT oi.name_snapshot, oi.quantity, oi.special_instructions,
                   ARRAY_AGG(m.name_snapshot) FILTER (WHERE m.id IS NOT NULL) AS mods
            FROM orders o JOIN order_items oi ON oi.order_id = o.id
            LEFT JOIN order_item_modifiers m ON m.order_item_id = oi.id
            WHERE o.order_number=$1 AND o.business_date=CURRENT_DATE
            GROUP BY oi.id ORDER BY oi.position
            """,
            out["order_number"],
        )
        status = await conn.fetchval(
            "SELECT status FROM orders WHERE order_number=$1"
            " AND business_date=CURRENT_DATE",
            out["order_number"],
        )

    assert status == "fired"
    assert rows[0]["name_snapshot"] == "Nashville Hot Chicken"
    assert rows[0]["quantity"] == 2
    assert rows[0]["special_instructions"] == "no pickles"
    assert rows[0]["mods"] == ["Extra Hot"]
    assert rows[1]["name_snapshot"] == "Mac and Cheese"


async def test_order_error_messages_are_speakable(kit):
    """These strings get read aloud to a customer. No stack traces, no jargon."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch("add_item", {"item_code": "truffle-risotto"})
    msg = r["error"]
    assert msg == msg.lower() or msg[0].isupper()
    assert "Traceback" not in msg and "psycopg" not in msg and "asyncpg" not in msg


async def test_quote_uses_integer_money_throughout(kit):
    """A float cent is a rounding bug waiting for a busy Friday."""
    pool = kit["pool"]
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Sweet Tea"),
            "modifier_codes": [modifier_id(kit["menu"], "Sweet Tea", "Large")],
            "quantity": 3,
        },
    )
    async with pool.acquire() as conn:
        q = await orders.quote(conn, order_id=d.order_id)
    assert isinstance(q.subtotal_cents, int)
    assert isinstance(q.tax_cents, int)
    assert q.total_cents == q.subtotal_cents + q.tax_cents


async def test_codes_are_short_enough_for_a_voice_model_to_reproduce(kit):
    """A native-audio model must emit these exactly in a function call.

    Long random tokens are where speech-to-speech models fail, which is why
    the menu stopped exposing uuids.
    """
    for cat in kit["menu"]:
        for it in cat["items"]:
            assert len(it["code"]) <= 24, it["code"]
            assert "-" not in it["code"][:1]
            assert it["code"].replace("-", "").isalnum()
            for g in it.get("modifier_groups", []):
                for o in g["options"]:
                    assert len(o["code"]) <= 26, o["code"]


async def test_a_uuid_is_no_longer_a_valid_item_reference(kit):
    """Guard against a half-finished migration leaving both paths alive."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch("add_item", {"item_code": str(uuid.uuid4())})
    assert "error" in r


async def test_item_codes_are_case_and_whitespace_tolerant(kit):
    """Models capitalise and pad things. That should not lose an order."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch("add_item", {"item_code": "  Smash-Burger  "})
    assert "error" not in r, r


async def test_snapshot_exposes_no_uuids_at_all(kit):
    """Every uuid in the prompt is tokens spent and a chance to hallucinate."""
    import json
    import re

    blob = json.dumps(kit["menu"])
    uuids = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", blob
    )
    assert uuids == [], f"snapshot still leaks uuids: {uuids[:2]}"


# ------------------------------------------------- when it goes off script


async def test_two_dead_ends_prompt_escalation(kit):
    """An agent still guessing after two failures sounds like it is not
    listening, which is when people hang up."""
    d = await kit["make"]()
    first = await d.dispatch("find_item", {"query": "pad thai"})
    assert "hint" not in first, "one miss is normal, not a crisis"
    second = await d.dispatch("find_item", {"query": "sushi"})
    assert "transfer_to_human" in second.get("hint", "")


async def test_a_success_resets_the_dead_end_counter(kit):
    """Someone who stumbles once early must not be escalated later for it."""
    d = await kit["make"]()
    await d.dispatch("find_item", {"query": "pad thai"})
    await d.dispatch("find_item", {"query": "the wings"})
    assert d.failed_attempts == 0
    assert "hint" not in await d.dispatch("find_item", {"query": "sushi"})


async def test_an_order_error_also_counts_as_a_dead_end(kit):
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    await d.dispatch("add_item", {"item_code": "not-a-dish"})
    r = await d.dispatch("add_item", {"item_code": "also-not-a-dish"})
    assert "hint" in r


async def test_check_open_reports_status_and_the_next_change(kit):
    d = await kit["make"]()
    r = await d.dispatch("check_open", {})
    assert "open" in r
    assert r.get("closes_at") or r.get("opens_at"), r


async def test_hours_handle_a_closed_day_and_a_late_close(kit):
    """Monday is closed and Friday runs an hour later. Both come from rows,
    so a holiday closure is data rather than a code change."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.agent import hours

    tz = kit["tenant"].timezone
    zone = ZoneInfo(tz)
    async with kit["pool"].acquire() as conn:
        monday = await hours.status(
            conn, kit["tenant"].id, tz, datetime(2026, 8, 10, 13, 0, tzinfo=zone)
        )
        friday_late = await hours.status(
            conn, kit["tenant"].id, tz, datetime(2026, 8, 14, 22, 30, tzinfo=zone)
        )
        tuesday_late = await hours.status(
            conn, kit["tenant"].id, tz, datetime(2026, 8, 11, 22, 30, tzinfo=zone)
        )

    assert monday["open"] is False
    assert friday_late["open"] is True, "Friday closes at 11pm"
    assert tuesday_late["open"] is False, "Tuesday closes at 10pm"
    assert monday["opens_at"], "a closed answer must say when we next open"


async def test_every_tool_parameter_is_described(kit):
    """A parameter with no description is one the model has to guess at, and
    it guesses by not using it. That reads to a caller as "I can't do that"."""
    from app.agent.tools import TOOL_SCHEMAS

    undescribed = [
        f"{t['name']}.{name}"
        for t in TOOL_SCHEMAS
        for name, spec in t["parameters"].get("properties", {}).items()
        if not spec.get("description")
    ]
    assert undescribed == [], undescribed


async def test_tool_schemas_stay_inside_the_supported_subset(kit):
    """Gemini accepts a subset of JSON Schema. An unsupported key can get the
    whole parameter dropped, which looks exactly like the model refusing."""
    from app.agent.tools import TOOL_SCHEMAS

    allowed = {
        "type", "format", "description", "nullable", "enum",
        "properties", "required", "items",
    }
    bad: list[str] = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "properties":
                for name, sub in value.items():
                    walk(sub, f"{path}.{name}")
            elif key == "items":
                walk(value, path + "[]")
            elif key not in allowed and path:
                bad.append(f"{path}: {key}")

    for tool in TOOL_SCHEMAS:
        walk(tool["parameters"], tool["name"])
    assert bad == [], bad


async def test_add_item_accepts_options_from_the_dish_own_groups(kit):
    """The path a caller asking for bacon actually takes."""
    d = await kit["make"]()
    await d.dispatch("start_order", {"order_type": "pickup"})
    r = await d.dispatch(
        "add_item",
        {
            "item_code": item_id(kit["menu"], "Smash Burger"),
            "quantity": 1,
            "modifier_codes": [
                modifier_id(kit["menu"], "Smash Burger", "Bacon"),
                modifier_id(kit["menu"], "Smash Burger", "Fried egg"),
            ],
        },
    )
    assert "error" not in r, r
    rev = await d.dispatch("review_order", {})
    assert "Bacon" in rev["spoken_summary"]


async def test_the_tenant_has_exactly_one_inbound_number(kit):
    """The seed used to re-add its placeholder on every deploy, leaving two
    numbers on the tenant and the portal reporting whichever was older."""
    async with kit["pool"].acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.e164 FROM phone_numbers p
            JOIN restaurants r ON r.id = p.restaurant_id
            WHERE r.slug = 'pilot' AND p.is_active
            """
        )
    assert len(rows) == 1, [r["e164"] for r in rows]
