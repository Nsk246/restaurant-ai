"""The tool layer.

Every action the agent can take is here, as a constrained schema plus a
dispatcher that validates against the database. The model passes uuids that
came out of the menu snapshot; it never passes a dish name as free text.

Errors are returned to the model as data, not raised. A tool failure should
become something the agent says out loud ("we're out of wings tonight"),
not a dropped call.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..kitchen import KitchenSink
from . import menu as menu_mod
from . import orders

log = logging.getLogger(__name__)

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "find_item",
        "description": (
            "Look up a menu item from what the caller said. Use this when you "
            "are not certain which item they mean. Returns candidates; if more "
            "than one comes back, ask the caller which they meant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the caller said"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "start_order",
        "description": "Begin a new order. Call once, before adding any items.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_type": {
                    "type": "string",
                    "enum": ["pickup", "delivery", "dine_in"],
                }
            },
            "required": ["order_type"],
        },
    },
    {
        "name": "add_item",
        "description": (
            "Add one item to the order. item_code and modifier_codes are the "
            "short codes in square brackets in the menu, like 'smash-burger'. "
            "Never invent one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_code": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "modifier_codes": {"type": "array", "items": {"type": "string"}},
                "note": {
                    "type": "string",
                    "description": "Free text for the kitchen, e.g. 'no pickles'",
                },
            },
            "required": ["item_code"],
        },
    },
    {
        "name": "change_quantity",
        "description": "Change how many of an existing line. Zero removes it.",
        "parameters": {
            "type": "object",
            "properties": {
                "line_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 0},
            },
            "required": ["line_id", "quantity"],
        },
    },
    {
        "name": "review_order",
        "description": (
            "Get the current order with a spoken summary. Read this back to "
            "the caller and get a yes before confirming."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "confirm_order",
        "description": (
            "Confirm after the caller has agreed to the readback. Only call "
            "this once they have said yes to review_order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "table_label": {"type": "string"},
            },
        },
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Hand the call to a person. Use when the caller asks, when you "
            "have failed to understand twice, or for anything you cannot do."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


class ToolDispatcher:
    """Executes tool calls for one conversation."""

    def __init__(
        self,
        pool,
        *,
        tenant: menu_mod.Tenant,
        menu: list[dict],
        conversation_id: str | None = None,
        channel: str = "phone",
        kitchen: KitchenSink | None = None,
    ):
        self.pool = pool
        self.tenant = tenant
        self.menu = menu
        self.conversation_id = conversation_id
        self.channel = channel
        self.kitchen = kitchen
        self.order_id: str | None = None
        self.customer_name: str | None = None
        self.reviewed = False
        self.transfer_requested: str | None = None

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return await handler(args)
        except orders.OrderError as exc:
            # Expected and speakable. The agent says this to the caller.
            return {"error": str(exc)}
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"error": "something went wrong on our end", "detail": str(exc)}

    # ------------------------------------------------------------------ tools

    async def _t_find_item(self, args: dict) -> dict:
        hits = menu_mod.find_candidates(self.menu, args.get("query", ""))
        return {
            "candidates": [
                {"code": h["code"], "name": h["name"], "price": h["price"]}
                for h in hits[:5]
            ]
        }

    async def _t_start_order(self, args: dict) -> dict:
        if self.order_id:
            return {"order_id": self.order_id, "note": "order already started"}
        async with self.pool.acquire() as conn:
            self.order_id = await orders.start_order(
                conn,
                restaurant_id=self.tenant.id,
                channel=self.channel,
                order_type=args["order_type"],
                conversation_id=self.conversation_id,
                idempotency_key=f"{self.conversation_id or uuid.uuid4()}:order",
            )
        return {"order_id": self.order_id}

    async def _t_add_item(self, args: dict) -> dict:
        if not self.order_id:
            await self._t_start_order({"order_type": "pickup"})
        # Any change invalidates a previous readback.
        self.reviewed = False
        async with self.pool.acquire() as conn:
            line_id = await orders.add_item(
                conn,
                order_id=self.order_id,
                restaurant_id=self.tenant.id,
                item_code=args["item_code"],
                quantity=int(args.get("quantity", 1)),
                modifier_codes=args.get("modifier_codes") or [],
                note=args.get("note"),
            )
            q = await orders.quote(conn, order_id=self.order_id)
        return {"line_id": line_id, "running_total": q.total_cents / 100}

    async def _t_change_quantity(self, args: dict) -> dict:
        if not self.order_id:
            return {"error": "no order in progress"}
        self.reviewed = False
        async with self.pool.acquire() as conn:
            await orders.set_quantity(
                conn,
                order_id=self.order_id,
                line_id=args["line_id"],
                quantity=int(args["quantity"]),
            )
            q = await orders.quote(conn, order_id=self.order_id)
        return {"running_total": q.total_cents / 100}

    async def _t_review_order(self, args: dict) -> dict:
        if not self.order_id:
            return {"error": "no order in progress"}
        async with self.pool.acquire() as conn:
            q = await orders.quote(conn, order_id=self.order_id)
        self.reviewed = True
        return {
            "spoken_summary": q.spoken(),
            "lines": [
                {
                    "line_id": ln.line_id,
                    "name": ln.name,
                    "quantity": ln.quantity,
                    "modifiers": ln.modifiers,
                    "note": ln.note,
                }
                for ln in q.lines
            ],
            "total": q.total_cents / 100,
            "quoted_minutes": q.prep_minutes,
        }

    async def _t_confirm_order(self, args: dict) -> dict:
        if not self.order_id:
            return {"error": "no order in progress"}
        if not self.reviewed:
            # Enforced here rather than trusted to the prompt. An unread order
            # is how wrong food gets made.
            return {"error": "read the order back with review_order and get a yes first"}
        self.customer_name = args.get("customer_name") or self.customer_name
        async with self.pool.acquire() as conn:
            result = await orders.confirm(
                conn, order_id=self.order_id, table_label=args.get("table_label")
            )
            if not result.get("already_confirmed"):
                await orders.fire(conn, order_id=self.order_id)
                q = await orders.quote(conn, order_id=self.order_id)
                ticket = {
                    "order_id": self.order_id,
                    "order_number": result["order_number"],
                    "customer_name": self.customer_name,
                    "lines": [
                        {
                            "name": ln.name,
                            "quantity": ln.quantity,
                            "modifiers": ln.modifiers,
                            "note": ln.note,
                        }
                        for ln in q.lines
                    ],
                }
                if self.kitchen:
                    await self.kitchen.fire(self.order_id, ticket)
        return {
            "order_number": result["order_number"],
            "total": (result.get("total_cents") or 0) / 100,
            "quoted_minutes": result.get("quoted_minutes"),
        }

    async def _t_transfer_to_human(self, args: dict) -> dict:
        self.transfer_requested = args.get("reason", "caller asked")
        return {
            "transferring": True,
            "to": self.tenant.transfer_phone,
            "say": "Let me get someone for you, one moment.",
        }
