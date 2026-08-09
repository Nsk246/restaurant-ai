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
                "query": {
                    "type": "string",
                    "description": "The caller's own words, e.g. 'the wings'.",
                }
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
                    "description": "How the caller is taking the order.",
                }
            },
            "required": ["order_type"],
        },
    },
    {
        "name": "add_item",
        "description": (
            "Add one item to the order, with any options the caller asked "
            "for. Use the short codes in square brackets in the menu. Never "
            "invent a code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_code": {
                    "type": "string",
                    "description": (
                        "The dish code from the menu, e.g. 'smash-burger'."
                    ),
                },
                "quantity": {
                    "type": "integer",
                    "description": "How many. Defaults to 1.",
                },
                "modifier_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Option codes from that dish's own groups, e.g. "
                        "['add-ons-bacon', 'add-ons-fried-egg'] for a burger "
                        "with bacon and a fried egg, or ['heat-lev-hot'] for "
                        "a heat level. Required groups must be answered here. "
                        "Use the codes listed under the dish, never one from "
                        "a different dish."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Anything the kitchen needs that is not an option "
                        "code, in the caller's own words, e.g. 'no pickles'."
                    ),
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
                "line_id": {
                    "type": "string",
                    "description": "The line_id returned by add_item.",
                },
                "quantity": {
                    "type": "integer",
                    "description": "The new quantity. Zero removes the line.",
                },
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
                "customer_name": {
                    "type": "string",
                    "description": "The name to put on the ticket.",
                },
                "table_label": {
                    "type": "string",
                    "description": "Table number. Required for dine-in.",
                },
            },
        },
    },
    {
        "name": "check_open",
        "description": (
            "Whether the restaurant is open right now, and when it next opens "
            "or closes. Call this before promising a pickup time if you are "
            "not sure, and whenever the caller asks about hours."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Hand the call to a person. Use when the caller asks, when you "
            "have failed to understand twice, or for anything you cannot do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why, in a few words. For the log, not the caller.",
                }
            },
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
        max_clarify_attempts: int = 2,
    ):
        self.pool = pool
        self.tenant = tenant
        self.menu = menu
        self.conversation_id = conversation_id
        self.channel = channel
        self.kitchen = kitchen
        self.max_clarify_attempts = max_clarify_attempts
        self.order_id: str | None = None
        self.customer_name: str | None = None
        self.reviewed = False
        self.transfer_requested: str | None = None
        # Consecutive things the agent could not resolve. Two is the point at
        # which continuing to guess is worse than fetching a person.
        self.failed_attempts = 0

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            result = await handler(args)
        except orders.OrderError as exc:
            # Expected and speakable. The agent says this to the caller.
            return self._track(name, {"error": str(exc)})
        except Exception as exc:
            log.exception("tool %s failed", name)
            return self._track(
                name, {"error": "something went wrong on our end", "detail": str(exc)}
            )
        # One line per tool call. Without it, "the model said it could not do
        # add-ons" is unfalsifiable: you cannot tell a refusal from a
        # rejected argument, and they need opposite fixes.
        if result.get("error"):
            log.warning("TOOL %s%s -> %s", name, args, result["error"])
        else:
            log.info("TOOL %s%s -> ok", name, args)
        return self._track(name, result)

    def _track(self, name: str, result: dict) -> dict:
        """Count consecutive dead ends and say when to stop guessing.

        An agent that keeps trying after two failures sounds like it is not
        listening, which is when people hang up. The hint is returned as data
        so the model can escalate in its own words rather than reciting ours.
        """
        stuck = bool(result.get("error")) or (
            name == "find_item" and not result.get("candidates")
        )
        if stuck:
            self.failed_attempts += 1
            if self.failed_attempts >= self.max_clarify_attempts:
                result["hint"] = (
                    "You have failed to help twice now. Stop guessing, "
                    "apologise briefly, and call transfer_to_human."
                )
        else:
            self.failed_attempts = 0
        return result

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

    async def _t_check_open(self, args: dict) -> dict:
        from . import hours

        async with self.pool.acquire() as conn:
            return await hours.status(conn, self.tenant.id, self.tenant.timezone)

    async def _t_transfer_to_human(self, args: dict) -> dict:
        self.transfer_requested = args.get("reason", "caller asked")
        return {
            "transferring": True,
            "to": self.tenant.transfer_phone,
            "say": "Let me get someone for you, one moment.",
        }
