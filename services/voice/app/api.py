"""Portal and kitchen display API.

Two audiences, deliberately different. The rail endpoints are read by a screen
six feet away in a hot kitchen, so they return few fields and no prose. The
call endpoints feed the front-of-house view during a demo.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from . import db, live
from .agent import menu as menu_mod
from .config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Screens watching the rail. Kept per-restaurant so a second tenant never
# sees another kitchen's tickets.
_rail_watchers: dict[str, set[WebSocket]] = {}


async def broadcast_rail(restaurant_id: str, payload: dict) -> None:
    dead = set()
    for ws in _rail_watchers.get(restaurant_id, set()):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.add(ws)
    _rail_watchers.get(restaurant_id, set()).difference_update(dead)


def _pool():
    """The pool, or a readable 503.

    The service boots without a database so /health can report the outage.
    Every other endpoint should then say so plainly rather than raising a
    RuntimeError that reaches the browser as an unexplained 500.
    """
    try:
        return db.pool()
    except RuntimeError as exc:
        raise HTTPException(503, "database unavailable") from exc


async def _tenant_id(conn, slug: str | None = None) -> str:
    """Single-tenant today. The lookup exists so M-anything-later is a config
    change rather than a rewrite."""
    row = await conn.fetchrow(
        "SELECT id FROM restaurants WHERE ($1::text IS NULL OR slug = $1)"
        " AND is_active ORDER BY created_at LIMIT 1",
        slug,
    )
    if row is None:
        raise HTTPException(404, "no active restaurant")
    return str(row["id"])


@router.get("/restaurant")
async def restaurant(slug: str | None = None) -> dict[str, Any]:
    async with _pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, slug, timezone, tax_bps FROM restaurants"
            " WHERE ($1::text IS NULL OR slug = $1) AND is_active"
            " ORDER BY created_at LIMIT 1",
            slug,
        )
        if row is None:
            raise HTTPException(404, "no active restaurant")
        number = await conn.fetchval(
            "SELECT e164 FROM phone_numbers WHERE restaurant_id=$1 AND is_active"
            " ORDER BY created_at LIMIT 1",
            row["id"],
        )
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "timezone": row["timezone"],
        "phone": number,
    }


@router.get("/menu")
async def menu(slug: str | None = None) -> dict[str, Any]:
    """Full menu including unavailable items, since the 86 toggle needs both."""
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        rows = await conn.fetch(
            """
            SELECT mi.code, mi.name, mi.price_cents, mi.is_available,
                   mi.is_active, c.name AS category, c.position AS cat_pos,
                   mi.position
            FROM menu_items mi
            JOIN menu_categories c ON c.id = mi.category_id
            WHERE mi.restaurant_id = $1
            ORDER BY c.position, mi.position
            """,
            rid,
        )
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["category"], []).append(
            {
                "code": r["code"],
                "name": r["name"],
                "price": r["price_cents"] / 100,
                "available": r["is_available"],
                "active": r["is_active"],
            }
        )
    return {"categories": [{"name": k, "items": v} for k, v in out.items()]}


@router.post("/menu/{code}/availability")
async def set_availability(code: str, available: bool, slug: str | None = None):
    """The 86 button. An item switched off here vanishes from the agent's
    menu on the next call, rather than being something the prompt must
    remember to avoid."""
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        updated = await conn.fetchrow(
            "UPDATE menu_items SET is_available=$3 WHERE restaurant_id=$1"
            " AND code=$2 RETURNING name, is_available",
            rid,
            code,
            available,
        )
        if updated is None:
            raise HTTPException(404, f"no menu item {code!r}")
    await broadcast_rail(
        rid,
        {
            "type": "availability",
            "code": code,
            "name": updated["name"],
            "available": updated["is_available"],
        },
    )
    return {"code": code, "available": updated["is_available"]}


@router.get("/rail")
async def rail(slug: str | None = None) -> dict[str, Any]:
    """Live tickets, oldest first. That order is the whole point of a rail."""
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        orders = await conn.fetch(
            """
            SELECT o.id, o.order_number, o.status, o.order_type, o.table_label,
                   o.fired_at, o.quoted_minutes, o.total_cents, o.customer_note,
                   c.name AS customer_name,
                   EXTRACT(EPOCH FROM (now() - COALESCE(o.fired_at, o.created_at)))::int
                       AS age_seconds
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            WHERE o.restaurant_id = $1
              AND o.status IN ('fired', 'preparing', 'ready')
            ORDER BY o.fired_at NULLS LAST, o.created_at
            """,
            rid,
        )
        lines = await conn.fetch(
            """
            SELECT oi.order_id, oi.name_snapshot, oi.quantity,
                   oi.special_instructions,
                   COALESCE(
                       ARRAY_AGG(m.name_snapshot) FILTER (WHERE m.id IS NOT NULL),
                       '{}'
                   ) AS mods
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            LEFT JOIN order_item_modifiers m ON m.order_item_id = oi.id
            WHERE o.restaurant_id = $1
              AND o.status IN ('fired', 'preparing', 'ready')
            GROUP BY oi.id
            ORDER BY oi.position
            """,
            rid,
        )
    by_order: dict[str, list] = {}
    for ln in lines:
        by_order.setdefault(str(ln["order_id"]), []).append(
            {
                "name": ln["name_snapshot"],
                "quantity": ln["quantity"],
                "modifiers": list(ln["mods"]),
                "note": ln["special_instructions"],
            }
        )
    return {
        "tickets": [
            {
                "id": str(o["id"]),
                "number": o["order_number"],
                "status": o["status"],
                "type": o["order_type"],
                "table": o["table_label"],
                "customer": o["customer_name"],
                "age_seconds": o["age_seconds"] or 0,
                "quoted_minutes": o["quoted_minutes"],
                "total": (o["total_cents"] or 0) / 100,
                "note": o["customer_note"],
                "lines": by_order.get(str(o["id"]), []),
            }
            for o in orders
        ]
    }


@router.post("/rail/{order_id}/advance")
async def advance(order_id: str, to: str, slug: str | None = None):
    """Move a ticket along the rail. The database rejects illegal moves, so
    a double tap on a busy screen cannot corrupt the order."""
    if to not in ("preparing", "ready", "completed", "cancelled"):
        raise HTTPException(400, f"cannot move a ticket to {to!r}")
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        try:
            row = await conn.fetchrow(
                "UPDATE orders SET status=$3::order_status WHERE id=$1"
                " AND restaurant_id=$2 RETURNING order_number, status",
                order_id,
                rid,
                to,
            )
        except Exception as exc:
            # The state machine trigger raises on an illegal transition.
            raise HTTPException(409, str(exc).split("\n")[0]) from exc
        if row is None:
            raise HTTPException(404, "no such ticket")
    await broadcast_rail(
        rid, {"type": "advance", "order_id": order_id, "status": row["status"]}
    )
    return {"order_id": order_id, "status": row["status"]}


@router.get("/calls")
async def calls(limit: int = 10, slug: str | None = None) -> dict[str, Any]:
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        rows = await conn.fetch(
            """
            SELECT id, external_id, from_e164, started_at, ended_at, outcome,
                   turn_count, p50_response_ms, p95_response_ms
            FROM conversations WHERE restaurant_id=$1
            ORDER BY started_at DESC LIMIT $2
            """,
            rid,
            min(limit, 50),
        )
    # Liveness comes from the connected sockets, not from a row that happens
    # to have no end time.
    connected = live.live_call_ids(rid)
    return {
        "calls": [
            {
                "id": str(r["id"]),
                "call_id": r["external_id"],
                "from": r["from_e164"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "live": r["external_id"] in connected,
                "outcome": r["outcome"],
                "turns": r["turn_count"],
                "p50_ms": r["p50_response_ms"],
                "p95_ms": r["p95_response_ms"],
            }
            for r in rows
        ]
    }


@router.post("/demo/reset")
async def demo_reset(slug: str | None = None):
    """Put the pilot back to a clean state between demos.

    Clears orders, conversations, and un-86s everything. Deliberately does
    not touch the menu itself, so a prospect's own menu survives a reset.
    """
    if not get_settings().demo_mode:
        raise HTTPException(403, "reset is only available in demo mode")
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        async with conn.transaction():
            await conn.execute("DELETE FROM orders WHERE restaurant_id=$1", rid)
            await conn.execute("DELETE FROM conversations WHERE restaurant_id=$1", rid)
            await conn.execute("DELETE FROM notifications WHERE restaurant_id=$1", rid)
            await conn.execute(
                "DELETE FROM order_number_counters WHERE restaurant_id=$1", rid
            )
            await conn.execute(
                "UPDATE menu_items SET is_available=true WHERE restaurant_id=$1", rid
            )
    live.clear()
    await broadcast_rail(rid, {"type": "reset"})
    return {"reset": True}


@router.websocket("/ws/rail")
async def rail_socket(ws: WebSocket, slug: str | None = None):
    """Pushes tickets to the kitchen screen as they fire."""
    await ws.accept()
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
    _rail_watchers.setdefault(rid, set()).add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _rail_watchers.get(rid, set()).discard(ws)


async def snapshot_for(restaurant_id: str) -> list[dict]:
    async with _pool().acquire() as conn:
        return await menu_mod.snapshot(conn, restaurant_id)
