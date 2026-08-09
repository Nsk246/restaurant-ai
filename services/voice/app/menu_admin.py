"""Menu management.

The menu was seed SQL, which meant a price change was a deploy. This is the
door a restaurant actually uses, and the same door a real menu gets imported
through during onboarding.

Two rules run through everything here:

* Codes are stable. They are what the agent emits in a tool call, so renaming
  a dish must not orphan it, and a code is never silently reassigned.
* Deleting is soft where history exists. An order from last Tuesday snapshots
  its own name and price, but removing a menu item the agent is mid-call with
  is a different matter, so items deactivate rather than vanish.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, menu_import
from .config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/menu")


def slugify(name: str, max_len: int = 24) -> str:
    """Matches the SQL slugify() so codes generated here and there agree."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:max_len]


def _pool():
    try:
        return db.pool()
    except RuntimeError as exc:
        raise HTTPException(503, "database unavailable") from exc


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    price: float = Field(ge=0)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    prep_minutes: int = Field(default=15, ge=0, le=240)


class ItemPatch(BaseModel):
    name: str | None = None
    price: float | None = Field(default=None, ge=0)
    description: str | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None
    prep_minutes: int | None = Field(default=None, ge=0, le=240)
    is_active: bool | None = None


async def _tenant_id(conn, slug: str | None = None) -> str:
    row = await conn.fetchrow(
        "SELECT id FROM restaurants WHERE ($1::text IS NULL OR slug = $1)"
        " AND is_active ORDER BY created_at LIMIT 1",
        slug,
    )
    if row is None:
        raise HTTPException(404, "no active restaurant")
    return str(row["id"])


async def _category_id(conn, restaurant_id: str, name: str) -> str:
    """Find or create. A restaurant adding a special should not have to think
    about whether the section already exists."""
    row = await conn.fetchrow(
        "SELECT id FROM menu_categories WHERE restaurant_id=$1 AND name=$2",
        restaurant_id,
        name.strip(),
    )
    if row:
        return str(row["id"])
    row = await conn.fetchrow(
        """
        INSERT INTO menu_categories (restaurant_id, name, position)
        VALUES ($1, $2, COALESCE(
            (SELECT MAX(position)+1 FROM menu_categories WHERE restaurant_id=$1), 0))
        RETURNING id
        """,
        restaurant_id,
        name.strip(),
    )
    return str(row["id"])


async def _unique_code(conn, restaurant_id: str, name: str) -> str:
    """A code the agent can say back. Collisions get a numeric suffix rather
    than an error, because two dishes can legitimately slugify the same."""
    base = slugify(name) or "item"
    code = base
    n = 1
    while await conn.fetchval(
        "SELECT 1 FROM menu_items WHERE restaurant_id=$1 AND code=$2",
        restaurant_id,
        code,
    ):
        n += 1
        code = f"{base}-{n}"
    return code


@router.post("/items")
async def create_item(item: ItemIn, slug: str | None = None) -> dict[str, Any]:
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        try:
            async with conn.transaction():
                cat = await _category_id(conn, rid, item.category)
                code = await _unique_code(conn, rid, item.name)
                row = await conn.fetchrow(
                    """
                    INSERT INTO menu_items (restaurant_id, category_id, code,
                                            name, description, price_cents,
                                            aliases, tags, prep_minutes, position)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, COALESCE(
                        (SELECT MAX(position)+1 FROM menu_items
                         WHERE category_id=$2), 0))
                    RETURNING code, name, price_cents
                    """,
                    rid,
                    cat,
                    code,
                    item.name.strip(),
                    item.description,
                    round(item.price * 100),
                    item.aliases,
                    item.tags,
                    item.prep_minutes,
                )
        except asyncpg.UniqueViolationError as exc:
            # A dish name is unique per restaurant. Surface that as a conflict
            # the UI can explain, not a 500 that looks like a bug.
            raise HTTPException(409, f"{item.name!r} is already on the menu") from exc
    return {
        "code": row["code"],
        "name": row["name"],
        "price": row["price_cents"] / 100,
    }


@router.patch("/items/{code}")
async def update_item(code: str, patch: ItemPatch, slug: str | None = None):
    """Only the fields sent are touched.

    The code never changes, even on rename: it is what the agent emits in a
    tool call, and reassigning it would break a call in progress.
    """
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "nothing to update")

    sets, values = [], []
    column = {
        "name": "name",
        "description": "description",
        "aliases": "aliases",
        "tags": "tags",
        "prep_minutes": "prep_minutes",
        "is_active": "is_active",
    }
    for key, value in fields.items():
        if key == "price":
            values.append(round(value * 100))
            sets.append(f"price_cents = ${len(values) + 2}")
        elif key in column:
            values.append(value)
            sets.append(f"{column[key]} = ${len(values) + 2}")

    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        try:
            row = await conn.fetchrow(
                f"UPDATE menu_items SET {', '.join(sets)}"  # noqa: S608
                " WHERE restaurant_id=$1 AND code=$2"
                " RETURNING code, name, price_cents, is_active, is_available",
                rid,
                code,
                *values,
            )
        except asyncpg.UniqueViolationError as exc:
            # Renaming onto an existing dish. A conflict, not a crash.
            raise HTTPException(
                409, f"another item is already called {fields.get('name')!r}"
            ) from exc
        if row is None:
            raise HTTPException(404, f"no menu item {code!r}")
    return {
        "code": row["code"],
        "name": row["name"],
        "price": row["price_cents"] / 100,
        "active": row["is_active"],
        "available": row["is_available"],
    }


@router.delete("/items/{code}")
async def delete_item(code: str, slug: str | None = None):
    """Deactivates rather than deletes when the item has history.

    Hard-deleting would orphan the line items on past orders, and those are
    what a restaurant's takings are reconciled against.
    """
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        item = await conn.fetchrow(
            "SELECT id FROM menu_items WHERE restaurant_id=$1 AND code=$2", rid, code
        )
        if item is None:
            raise HTTPException(404, f"no menu item {code!r}")
        used = await conn.fetchval(
            "SELECT COUNT(*) FROM order_items WHERE menu_item_id=$1", item["id"]
        )
        if used:
            await conn.execute(
                "UPDATE menu_items SET is_active=false WHERE id=$1", item["id"]
            )
            return {"code": code, "deactivated": True, "orders": used}
        await conn.execute("DELETE FROM menu_items WHERE id=$1", item["id"])
    return {"code": code, "deleted": True}


@router.post("/categories")
async def create_category(name: str, slug: str | None = None):
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        cat = await _category_id(conn, rid, name)
    return {"id": cat, "name": name}


@router.post("/items/{code}/modifiers")
async def attach_modifier_group(
    code: str, group: str, slug: str | None = None
) -> dict[str, Any]:
    """Attach an existing modifier group to an item, by group name."""
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        item = await conn.fetchval(
            "SELECT id FROM menu_items WHERE restaurant_id=$1 AND code=$2", rid, code
        )
        if item is None:
            raise HTTPException(404, f"no menu item {code!r}")
        grp = await conn.fetchval(
            "SELECT id FROM modifier_groups WHERE restaurant_id=$1 AND name=$2",
            rid,
            group,
        )
        if grp is None:
            raise HTTPException(404, f"no modifier group {group!r}")
        await conn.execute(
            "INSERT INTO menu_item_modifier_groups (menu_item_id, modifier_group_id)"
            " VALUES ($1, $2) ON CONFLICT DO NOTHING",
            item,
            grp,
        )
    return {"code": code, "group": group, "attached": True}


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class CommitIn(BaseModel):
    items: list[menu_import.ParsedItem]
    replace: bool = False


@router.post("/import/preview")
async def preview_import(body: ImportIn) -> dict[str, Any]:
    """Structure a pasted menu without writing anything.

    Parsing menus is inexact: prices attach to the wrong dish, headings become
    items, extras look like plates. So this proposes, and a separate commit
    writes. Nobody should discover a mangled import by hearing the agent
    offer it to a customer.
    """
    settings = get_settings()
    if settings.gemini_api_key:
        items = await menu_import.parse_with_model(
            body.text, settings.gemini_api_key, settings.gemini_import_model
        )
        source = "model"
    else:
        items = menu_import.parse_plain(body.text)
        source = "rules"

    items = menu_import.clean_aliases(menu_import.dedupe(items))
    return {
        "source": source,
        "count": len(items),
        "items": [i.model_dump() for i in items],
        "missing_price": [i.name for i in items if i.price is None],
    }


@router.post("/import/commit")
async def commit_import(body: CommitIn, slug: str | None = None) -> dict[str, Any]:
    """Write the reviewed items.

    `replace` deactivates everything currently on the menu first, for onboarding
    a real restaurant over the sample data. It deactivates rather than deletes,
    so past orders still resolve their line items.
    """
    priced = [i for i in body.items if i.price is not None]
    skipped = [i.name for i in body.items if i.price is None]

    created, failed = [], []
    async with _pool().acquire() as conn:
        rid = await _tenant_id(conn, slug)
        async with conn.transaction():
            if body.replace:
                await conn.execute(
                    "UPDATE menu_items SET is_active=false WHERE restaurant_id=$1", rid
                )
            for item in priced:
                try:
                    cat = await _category_id(conn, rid, item.category or "Menu")
                    code = await _unique_code(conn, rid, item.name)
                    await conn.execute(
                        """
                        INSERT INTO menu_items (restaurant_id, category_id, code,
                                                name, description, price_cents,
                                                aliases, tags, position)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE(
                            (SELECT MAX(position)+1 FROM menu_items
                             WHERE category_id=$2), 0))
                        """,
                        rid,
                        cat,
                        code,
                        item.name.strip(),
                        item.description,
                        round(item.price * 100),
                        item.aliases,
                        item.tags,
                    )
                    created.append(code)
                except Exception as exc:
                    log.warning("could not import %r: %s", item.name, exc)
                    failed.append(item.name)

    return {
        "created": len(created),
        "codes": created,
        "skipped_no_price": skipped,
        "failed": failed,
    }
