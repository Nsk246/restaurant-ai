"""Tenant resolution and the menu snapshot injected into the agent context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass
class Tenant:
    id: str
    name: str
    timezone: str
    tax_bps: int
    transfer_phone: str | None
    config: dict[str, Any]


async def resolve_tenant(conn: asyncpg.Connection, dialled_e164: str) -> Tenant | None:
    """Which restaurant does this number belong to.

    The dialled number is the tenant router. One row per number, so a second
    restaurant needs no code change, only a row.
    """
    row = await conn.fetchrow(
        """
        SELECT r.id, r.name, r.timezone, r.tax_bps, r.transfer_phone_e164, r.agent_config
        FROM phone_numbers p
        JOIN restaurants r ON r.id = p.restaurant_id
        WHERE p.e164 = $1 AND p.is_active AND r.is_active
        """,
        dialled_e164,
    )
    if row is None:
        return None
    cfg = row["agent_config"]
    return Tenant(
        id=str(row["id"]),
        name=row["name"],
        timezone=row["timezone"],
        tax_bps=row["tax_bps"],
        transfer_phone=row["transfer_phone_e164"],
        config=json.loads(cfg) if isinstance(cfg, str) else dict(cfg or {}),
    )


async def snapshot(conn: asyncpg.Connection, restaurant_id: str) -> list[dict]:
    """The sellable menu. 86'd and inactive items are absent, not flagged."""
    raw = await conn.fetchval("SELECT menu_snapshot($1)", restaurant_id)
    return json.loads(raw) if isinstance(raw, str) else (raw or [])


def render_for_prompt(menu: list[dict]) -> str:
    """Compact text form. Ids are included because tools take ids, not names."""
    out = []
    for cat in menu:
        out.append(f"\n## {cat['category']}")
        for it in cat["items"]:
            line = f"- [{it['code']}] {it['name']} ${it['price']:.2f}"
            if it.get("aliases"):
                line += f" (also called: {', '.join(it['aliases'])})"
            if it.get("tags"):
                line += f" [{', '.join(it['tags'])}]"
            out.append(line)
            for g in it.get("modifier_groups", []):
                req = "required" if g["required"] else "optional"
                opts = ", ".join(
                    f"[{o['code']}] {o['name']}"
                    + (f" +${o['price_delta']:.2f}" if o["price_delta"] else "")
                    for o in g["options"]
                )
                out.append(f"    {g['name']} ({req}, max {g['max']}): {opts}")
    return "\n".join(out)


def find_candidates(menu: list[dict], query: str) -> list[dict]:
    """Match a spoken phrase to menu items by name or alias.

    Phone audio is 8kHz and callers say "the wings", not "Nashville Hot Wings".
    Used to disambiguate out loud, never to pick silently.
    """
    q = query.strip().lower()
    if not q:
        return []
    hits = []
    for cat in menu:
        for it in cat["items"]:
            names = [it["name"].lower(), *(a.lower() for a in it.get("aliases", []))]
            if any(q == n for n in names):
                hits.append((0, it))
            elif any(q in n or n in q for n in names):
                hits.append((1, it))
    hits.sort(key=lambda h: h[0])
    return [h[1] for h in hits]
