"""Order operations.

Everything the agent can do to an order goes through here. The model never
writes free text into these tables: it passes menu and modifier uuids that
came out of the snapshot it was given, and this module validates them against
the database before anything is stored.

That constraint is the whole accuracy strategy. A model cannot invent a dish
it was never shown, and it cannot sell something the kitchen has 86'd, because
unavailable items never enter the snapshot in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import asyncpg


class OrderError(Exception):
    """Something the agent asked for is not allowed. The message is written to
    be spoken aloud, because that is where it ends up."""


@dataclass
class Line:
    line_id: str
    name: str
    quantity: int
    unit_price_cents: int
    modifiers: list[str] = field(default_factory=list)
    modifier_delta_cents: int = 0
    note: str | None = None

    @property
    def total_cents(self) -> int:
        return (self.unit_price_cents + self.modifier_delta_cents) * self.quantity


@dataclass
class Quote:
    lines: list[Line]
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    prep_minutes: int

    def spoken(self) -> str:
        """The readback. Every order is confirmed out loud before it fires."""
        parts = []
        for ln in self.lines:
            bit = f"{ln.quantity} {ln.name}"
            if ln.modifiers:
                bit += f" with {', '.join(ln.modifiers)}"
            if ln.note:
                bit += f", {ln.note}"
            parts.append(bit)
        items = "; ".join(parts)
        return (
            f"{items}. Total {self.total_cents / 100:.2f}, "
            f"about {self.prep_minutes} minutes."
        )


async def start_order(
    conn: asyncpg.Connection,
    *,
    restaurant_id: str,
    channel: str,
    order_type: str,
    conversation_id: str | None = None,
    idempotency_key: str,
) -> str:
    """Create a draft. Drafts are invisible to the kitchen by construction."""
    if order_type not in ("pickup", "delivery", "dine_in"):
        raise OrderError(f"unknown order type {order_type!r}")
    row = await conn.fetchrow(
        """
        INSERT INTO orders (restaurant_id, conversation_id, channel, order_type,
                            idempotency_key)
        VALUES ($1, $2, $3::channel, $4::order_type, $5)
        ON CONFLICT (restaurant_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        restaurant_id,
        conversation_id,
        channel,
        order_type,
        idempotency_key,
    )
    if row is None:
        existing = await conn.fetchrow(
            "SELECT id FROM orders WHERE restaurant_id=$1 AND idempotency_key=$2",
            restaurant_id,
            idempotency_key,
        )
        return str(existing["id"])
    return str(row["id"])


async def add_item(
    conn: asyncpg.Connection,
    *,
    order_id: str,
    restaurant_id: str,
    menu_item_id: str,
    quantity: int = 1,
    modifier_ids: list[str] | None = None,
    note: str | None = None,
) -> str:
    """Add one line. Validates the item and every modifier against the menu."""
    if quantity < 1:
        raise OrderError("quantity must be at least one")

    item = await conn.fetchrow(
        """
        SELECT id, name, price_cents, is_available, is_active, prep_minutes
        FROM menu_items WHERE id = $1 AND restaurant_id = $2
        """,
        menu_item_id,
        restaurant_id,
    )
    if item is None:
        raise OrderError("that item is not on the menu")
    if not item["is_active"]:
        raise OrderError(f"{item['name']} is not on the menu right now")
    if not item["is_available"]:
        raise OrderError(f"we are out of {item['name']} tonight")

    mods = await _validate_modifiers(conn, menu_item_id, modifier_ids or [])

    line = await conn.fetchrow(
        """
        INSERT INTO order_items (order_id, menu_item_id, name_snapshot,
                                 unit_price_cents, quantity, special_instructions,
                                 position)
        VALUES ($1, $2, $3, $4, $5, $6,
                COALESCE((SELECT MAX(position)+1 FROM order_items WHERE order_id=$1), 0))
        RETURNING id
        """,
        order_id,
        menu_item_id,
        item["name"],
        item["price_cents"],
        quantity,
        note,
    )
    line_id = line["id"]

    for m in mods:
        await conn.execute(
            """
            INSERT INTO order_item_modifiers (order_item_id, modifier_id,
                                              name_snapshot, price_delta_cents)
            VALUES ($1, $2, $3, $4)
            """,
            line_id,
            m["id"],
            m["name"],
            m["price_delta_cents"],
        )
    return str(line_id)


async def _validate_modifiers(
    conn: asyncpg.Connection, menu_item_id: str, modifier_ids: list[str]
) -> list[asyncpg.Record]:
    """Modifiers must belong to this item's groups, be available, and satisfy
    each group's min and max selection rules."""
    if not modifier_ids:
        await _check_required_groups(conn, menu_item_id, [])
        return []

    rows = await conn.fetch(
        """
        SELECT m.id, m.name, m.price_delta_cents, m.is_available,
               mg.id AS group_id, mg.name AS group_name,
               mg.min_select, mg.max_select
        FROM modifiers m
        JOIN modifier_groups mg ON mg.id = m.modifier_group_id
        JOIN menu_item_modifier_groups link
          ON link.modifier_group_id = mg.id AND link.menu_item_id = $1
        WHERE m.id = ANY($2::uuid[])
        """,
        menu_item_id,
        modifier_ids,
    )
    found = {str(r["id"]) for r in rows}
    missing = [m for m in modifier_ids if m not in found]
    if missing:
        raise OrderError("one of those options is not available for that item")
    for r in rows:
        if not r["is_available"]:
            raise OrderError(f"we are out of {r['name']}")

    per_group: dict[Any, list[asyncpg.Record]] = {}
    for r in rows:
        per_group.setdefault(r["group_id"], []).append(r)
    for group_rows in per_group.values():
        g = group_rows[0]
        if len(group_rows) > g["max_select"]:
            raise OrderError(
                f"you can pick at most {g['max_select']} for {g['group_name']}"
            )

    await _check_required_groups(conn, menu_item_id, list(per_group.keys()))
    return rows


async def _check_required_groups(
    conn: asyncpg.Connection, menu_item_id: str, chosen_group_ids: list[Any]
) -> None:
    required = await conn.fetch(
        """
        SELECT mg.id, mg.name, mg.prompt, mg.min_select
        FROM menu_item_modifier_groups link
        JOIN modifier_groups mg ON mg.id = link.modifier_group_id
        WHERE link.menu_item_id = $1
          AND COALESCE(link.is_required, mg.min_select > 0)
        """,
        menu_item_id,
    )
    chosen = {str(g) for g in chosen_group_ids}
    for r in required:
        if str(r["id"]) not in chosen:
            raise OrderError(r["prompt"] or f"which {r['name']} would you like?")


async def remove_line(conn: asyncpg.Connection, *, order_id: str, line_id: str) -> None:
    deleted = await conn.execute(
        "DELETE FROM order_items WHERE id = $1 AND order_id = $2", line_id, order_id
    )
    if deleted.endswith("0"):
        raise OrderError("that item is not on the order")


async def set_quantity(
    conn: asyncpg.Connection, *, order_id: str, line_id: str, quantity: int
) -> None:
    """Callers change their minds mid-order constantly. This is that path."""
    if quantity < 1:
        return await remove_line(conn, order_id=order_id, line_id=line_id)
    updated = await conn.execute(
        "UPDATE order_items SET quantity=$3 WHERE id=$1 AND order_id=$2",
        line_id,
        order_id,
        quantity,
    )
    if updated.endswith("0"):
        raise OrderError("that item is not on the order")


async def quote(conn: asyncpg.Connection, *, order_id: str) -> Quote:
    rows = await conn.fetch(
        """
        SELECT oi.id, oi.name_snapshot, oi.quantity, oi.unit_price_cents,
               oi.special_instructions,
               COALESCE(SUM(m.price_delta_cents), 0)::int AS mod_delta,
               COALESCE(ARRAY_AGG(m.name_snapshot) FILTER (WHERE m.id IS NOT NULL),
                        '{}') AS mod_names,
               mi.prep_minutes
        FROM order_items oi
        LEFT JOIN order_item_modifiers m ON m.order_item_id = oi.id
        LEFT JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE oi.order_id = $1
        GROUP BY oi.id, mi.prep_minutes
        ORDER BY oi.position
        """,
        order_id,
    )
    tax_bps = await conn.fetchval(
        """
        SELECT r.tax_bps FROM orders o
        JOIN restaurants r ON r.id = o.restaurant_id WHERE o.id = $1
        """,
        order_id,
    )

    lines = [
        Line(
            line_id=str(r["id"]),
            name=r["name_snapshot"],
            quantity=r["quantity"],
            unit_price_cents=r["unit_price_cents"],
            modifiers=list(r["mod_names"]),
            modifier_delta_cents=r["mod_delta"],
            note=r["special_instructions"],
        )
        for r in rows
    ]
    subtotal = sum(ln.total_cents for ln in lines)
    # Integer arithmetic throughout. Floats and money do not mix.
    tax = (subtotal * (tax_bps or 0)) // 10000
    prep = max((r["prep_minutes"] or 0) for r in rows) if rows else 0
    return Quote(lines, subtotal, tax, subtotal + tax, prep)


async def confirm(
    conn: asyncpg.Connection,
    *,
    order_id: str,
    table_label: str | None = None,
    delivery_address: dict | None = None,
) -> dict:
    """Draft to confirmed. Allocates the number the kitchen and caller share.

    Idempotent: confirming an already-confirmed order returns the same result
    instead of raising, because a caller repeating "yes" must not produce an
    error or a second ticket.
    """
    import json

    existing = await conn.fetchrow(
        "SELECT status, order_number, business_date FROM orders WHERE id=$1", order_id
    )
    if existing is None:
        raise OrderError("that order no longer exists")
    if existing["status"] != "draft":
        return {
            "order_id": order_id,
            "order_number": existing["order_number"],
            "already_confirmed": True,
        }

    line_count = await conn.fetchval(
        "SELECT COUNT(*) FROM order_items WHERE order_id=$1", order_id
    )
    if not line_count:
        raise OrderError("there is nothing on the order yet")

    q = await quote(conn, order_id=order_id)
    restaurant_id = await conn.fetchval(
        "SELECT restaurant_id FROM orders WHERE id=$1", order_id
    )
    today = date.today()
    number = await conn.fetchval("SELECT next_order_number($1, $2)", restaurant_id, today)

    await conn.execute(
        """
        UPDATE orders SET status='confirmed', order_number=$2, business_date=$3,
               subtotal_cents=$4, tax_cents=$5, total_cents=$6,
               quoted_minutes=$7, table_label=COALESCE($8, table_label),
               delivery_address=COALESCE($9::jsonb, delivery_address)
        WHERE id=$1
        """,
        order_id,
        number,
        today,
        q.subtotal_cents,
        q.tax_cents,
        q.total_cents,
        q.prep_minutes,
        table_label,
        json.dumps(delivery_address) if delivery_address else None,
    )
    return {
        "order_id": order_id,
        "order_number": number,
        "total_cents": q.total_cents,
        "quoted_minutes": q.prep_minutes,
        "already_confirmed": False,
    }


async def fire(conn: asyncpg.Connection, *, order_id: str) -> dict:
    """Confirmed to fired. This is the point of no return for the kitchen."""
    status = await conn.fetchval("SELECT status FROM orders WHERE id=$1", order_id)
    if status == "fired":
        return {"order_id": order_id, "already_fired": True}
    if status != "confirmed":
        raise OrderError(f"cannot fire an order that is {status}")
    await conn.execute("UPDATE orders SET status='fired' WHERE id=$1", order_id)
    return {"order_id": order_id, "already_fired": False}
