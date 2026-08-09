#!/usr/bin/env python3
"""Put believable tickets on the rail without making a phone call.

Runs real orders through the real tool layer, so what lands is exactly what a
call would produce, including the readback gate and the fired ticket. Used to
rehearse the demo and to have something on screen while working on the portal.

    python tools/seed_demo_calls.py            # three orders
    python tools/seed_demo_calls.py --count 6
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.agent import menu as menu_mod
from app.agent import session as session_mod
from app.agent.tools import ToolDispatcher

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://operator:operator@127.0.0.1:5432/operator"
)

# Written to exercise the things that actually go wrong on the phone:
# modifiers, kitchen notes, allergies, and a dine-in that needs a table.
BASKETS: list[tuple[str, list[tuple[str, int, list[str], str | None]]]] = [
    (
        "pickup",
        [
            ("nashville-hot-chicken", 2, ["heat-lev-extra-hot"], "no pickles"),
            ("mac-and-cheese", 1, [], None),
        ],
    ),
    (
        "pickup",
        [
            ("smash-burger", 1, ["add-ons-bacon", "add-ons-fried-egg"], "medium"),
            ("fries", 2, [], None),
        ],
    ),
    (
        "dine_in",
        [
            ("garden-grain-bowl", 1, [], "allergic to nuts, please keep separate"),
            ("sweet-tea", 2, ["drink-si-large"], None),
        ],
    ),
    (
        "pickup",
        [
            ("nashville-hot-wings", 1, ["heat-lev-medium"], None),
            ("pimento-cheese-dip", 1, [], "sauce on the side"),
            ("lemonade", 1, ["drink-si-regular"], None),
        ],
    ),
    (
        "pickup",
        [("blackened-catfish", 1, [], None), ("collard-greens", 1, [], None)],
    ),
]


async def main(count: int) -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        number = await conn.fetchval(
            "SELECT e164 FROM phone_numbers WHERE is_active ORDER BY created_at LIMIT 1"
        )
        if number is None:
            print("no phone number seeded; run: make seed", file=sys.stderr)
            return
        tenant = await menu_mod.resolve_tenant(conn, number)
        snapshot = await menu_mod.snapshot(conn, tenant.id)

    for i in range(count):
        order_type, basket = BASKETS[i % len(BASKETS)]
        caller = f"+1615555{random.randint(1000, 9999)}"

        async with pool.acquire() as conn:
            conversation_id = await session_mod.open_conversation(
                conn,
                restaurant_id=tenant.id,
                channel="phone",
                external_id=f"demo-{uuid.uuid4()}",
                from_e164=caller,
            )
            customer_id = await session_mod.upsert_customer(
                conn, restaurant_id=tenant.id, phone_e164=caller, name="Demo caller"
            )
            await conn.execute(
                "UPDATE orders SET customer_id=$2 WHERE conversation_id=$1",
                conversation_id,
                customer_id,
            )

        d = ToolDispatcher(
            pool, tenant=tenant, menu=snapshot, conversation_id=conversation_id
        )
        await d.dispatch("start_order", {"order_type": order_type})
        for code, qty, mods, note in basket:
            result = await d.dispatch(
                "add_item",
                {
                    "item_code": code,
                    "quantity": qty,
                    "modifier_codes": mods,
                    "note": note,
                },
            )
            if "error" in result:
                print(f"  {code}: {result['error']}")

        await d.dispatch("review_order", {})
        confirm_args = {"customer_name": "Demo caller"}
        if order_type == "dine_in":
            confirm_args["table_label"] = str(random.randint(1, 20))
        out = await d.dispatch("confirm_order", confirm_args)

        if "error" in out:
            print(f"order {i + 1}: {out['error']}")
            continue

        # Close the conversation, exactly as a real call does on hang-up.
        # Without this the history shows no outcome and no latency, which
        # looks like a broken portal rather than a seeding shortcut.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET customer_id=$2 WHERE conversation_id=$1",
                conversation_id,
                customer_id,
            )
            await session_mod.close_conversation(
                conn,
                conversation_id,
                outcome="order_placed",
                transcript=[
                    {"role": "caller", "text": "hi, can I place an order for pickup"},
                    {"role": "agent", "text": "of course, what can I get you?"},
                ],
                stats={
                    "turn_count": random.randint(4, 9),
                    "p50_response_ms": random.randint(520, 880),
                    "p95_response_ms": random.randint(900, 1400),
                },
            )
        print(f"fired #{out['order_number']}  {order_type}  ${out['total']:.2f}")

    await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3)
    asyncio.run(main(ap.parse_args().count))
