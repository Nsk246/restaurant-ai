"""Where a fired order goes.

The pilot restaurant's POS is unknown, so every kitchen write goes through
this interface with one implementation today. A Toast or Square adapter drops
in behind the same three methods without the agent changing.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class KitchenSink(Protocol):
    async def fire(self, order_id: str, ticket: dict) -> None: ...
    async def cancel(self, order_id: str, reason: str) -> None: ...
    async def health(self) -> bool: ...


class InternalKDS:
    """Our own kitchen display. The order is already in Postgres by the time
    this runs, so firing is a notification, not a write. That ordering is
    deliberate: if the notification fails the ticket still exists."""

    def __init__(self, notify=None):
        self._notify = notify

    async def fire(self, order_id: str, ticket: dict) -> None:
        log.info("fired order %s to the rail", ticket.get("order_number", order_id))
        if self._notify:
            await self._notify({"type": "ticket", **ticket})

    async def cancel(self, order_id: str, reason: str) -> None:
        log.info("cancelled order %s: %s", order_id, reason)
        if self._notify:
            await self._notify({"type": "ticket_cancelled", "order_id": order_id})

    async def health(self) -> bool:
        return True
