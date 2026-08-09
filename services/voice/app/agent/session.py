"""Conversation records and live call state.

A conversation row exists for every call and kiosk session, and orders point
at it. Creating it up front rather than lazily means the transcript, the
latency numbers, and the order all hang off the same id even if the call ends
badly.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg


async def open_conversation(
    conn: asyncpg.Connection,
    *,
    restaurant_id: str,
    channel: str = "phone",
    external_id: str | None = None,
    from_e164: str | None = None,
    recording_disclosed: bool = False,
) -> str:
    """Create the conversation, or return the existing one for this call.

    Idempotent on (restaurant_id, external_id) so a Twilio reconnect resumes
    the same conversation instead of forking a second one.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO conversations (restaurant_id, channel, external_id, from_e164,
                                   recording_disclosed)
        VALUES ($1, $2::channel, $3, $4, $5)
        ON CONFLICT (restaurant_id, external_id)
          DO UPDATE SET external_id = EXCLUDED.external_id
        RETURNING id
        """,
        restaurant_id,
        channel,
        external_id,
        from_e164,
        recording_disclosed,
    )
    return str(row["id"])


async def close_conversation(
    conn: asyncpg.Connection,
    conversation_id: str,
    *,
    outcome: str | None = None,
    transcript: list[dict] | None = None,
    stats: dict[str, Any] | None = None,
    transferred: bool = False,
    error_detail: str | None = None,
) -> None:
    stats = stats or {}
    await conn.execute(
        """
        UPDATE conversations
        SET ended_at = now(),
            outcome = COALESCE($2::conversation_outcome, outcome),
            transcript = COALESCE($3::jsonb, transcript),
            turn_count = COALESCE($4, turn_count),
            p50_response_ms = COALESCE($5, p50_response_ms),
            p95_response_ms = COALESCE($6, p95_response_ms),
            p50_model_ms = COALESCE($9, p50_model_ms),
            p50_transport_ms = COALESCE($10, p50_transport_ms),
            transferred_to_human = $7,
            error_detail = COALESCE($8, error_detail)
        WHERE id = $1
        """,
        conversation_id,
        outcome,
        json.dumps(transcript) if transcript is not None else None,
        stats.get("turn_count"),
        stats.get("p50_response_ms"),
        stats.get("p95_response_ms"),
        transferred,
        error_detail,
        stats.get("p50_model_ms"),
        stats.get("p50_transport_ms"),
    )


async def upsert_customer(
    conn: asyncpg.Connection,
    *,
    restaurant_id: str,
    phone_e164: str | None,
    name: str | None = None,
) -> str | None:
    """Recognise a returning caller by number. Nothing to do without one."""
    if not phone_e164:
        return None
    row = await conn.fetchrow(
        """
        INSERT INTO customers (restaurant_id, phone_e164, name, last_seen_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (restaurant_id, phone_e164) DO UPDATE
          SET last_seen_at = now(),
              name = COALESCE(EXCLUDED.name, customers.name)
        RETURNING id
        """,
        restaurant_id,
        phone_e164,
        name,
    )
    return str(row["id"])
