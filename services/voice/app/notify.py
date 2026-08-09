"""Outbound SMS.

Two rules encoded here:

* Nothing is sent twice. Every message carries an idempotency key, so a retry
  after a crash is a rejected insert rather than a second text to a customer.
* Demo mode allowlists recipients. The Twilio trial only delivers to verified
  numbers, and a demo that fails halfway through with a Twilio error in front
  of a prospect is worse than one that quietly queues.
"""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)


def confirmation_body(
    restaurant: str, order_number: int, total_cents: int, minutes: int | None
) -> str:
    """Short on purpose. This is read on a lock screen, one-handed."""
    when = f" Ready in about {minutes} min." if minutes else ""
    return (
        f"{restaurant}: order #{order_number} confirmed. "
        f"${total_cents / 100:.2f}.{when}"
    )


async def queue(
    conn: asyncpg.Connection,
    *,
    restaurant_id: str,
    to_e164: str,
    body: str,
    template: str,
    idempotency_key: str,
    order_id: str | None = None,
    customer_id: str | None = None,
) -> str | None:
    """Record the message. Returns None if this exact message already exists."""
    row = await conn.fetchrow(
        """
        INSERT INTO notifications (restaurant_id, customer_id, order_id, channel,
                                   to_address, template, body, idempotency_key)
        VALUES ($1, $2, $3, 'sms', $4, $5, $6, $7)
        ON CONFLICT (restaurant_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        restaurant_id,
        customer_id,
        order_id,
        to_e164,
        template,
        body,
        idempotency_key,
    )
    return str(row["id"]) if row else None


class SmsSender:
    """Sends queued messages. Never raises into the call path.

    A failed text must not fail an order that the kitchen is already cooking.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_e164: str,
        *,
        demo_mode: bool = True,
        allowlist: list[str] | None = None,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_e164 = from_e164
        self.demo_mode = demo_mode
        self.allowlist = {n.strip() for n in (allowlist or []) if n.strip()}

    def blocked_reason(self, to_e164: str) -> str | None:
        if not (self.account_sid and self.auth_token and self.from_e164):
            return "twilio credentials not configured"
        if self.demo_mode and self.allowlist and to_e164 not in self.allowlist:
            # The trial only delivers to verified numbers. Better to say so
            # in the log than to surface a Twilio error mid-demo.
            return "demo mode: recipient not on the allowlist"
        return None

    async def send_pending(self, conn: asyncpg.Connection, restaurant_id: str) -> int:
        rows = await conn.fetch(
            "SELECT id, to_address, body FROM notifications"
            " WHERE restaurant_id=$1 AND status='queued' ORDER BY created_at LIMIT 20",
            restaurant_id,
        )
        sent = 0
        for r in rows:
            reason = self.blocked_reason(r["to_address"])
            if reason:
                await conn.execute(
                    "UPDATE notifications SET status='failed', error_detail=$2,"
                    " attempts=attempts+1 WHERE id=$1",
                    r["id"],
                    reason,
                )
                log.info("sms not sent to %s: %s", r["to_address"], reason)
                continue
            try:
                sid = await self._deliver(r["to_address"], r["body"])
                await conn.execute(
                    "UPDATE notifications SET status='sent', provider_sid=$2,"
                    " sent_at=now(), attempts=attempts+1 WHERE id=$1",
                    r["id"],
                    sid,
                )
                sent += 1
            except Exception as exc:
                await conn.execute(
                    "UPDATE notifications SET status='failed', error_detail=$2,"
                    " attempts=attempts+1 WHERE id=$1",
                    r["id"],
                    f"{type(exc).__name__}: {exc}",
                )
                log.warning("sms delivery failed: %s", exc)
        return sent

    async def _deliver(self, to_e164: str, body: str) -> str:
        import asyncio

        from twilio.rest import Client

        def _send():
            client = Client(self.account_sid, self.auth_token)
            msg = client.messages.create(to=to_e164, from_=self.from_e164, body=body)
            return msg.sid

        # The Twilio SDK is synchronous; keep it off the event loop that is
        # also pumping call audio.
        return await asyncio.to_thread(_send)
