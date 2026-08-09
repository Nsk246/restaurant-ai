"""Turn a pasted menu into structured items.

Onboarding a restaurant means getting 80 dishes with prices and modifiers into
the database. Nobody types that in, and nobody should have to write SQL for
it. This takes whatever they have — a copy-pasted PDF, an email, a photo
transcribed — and proposes structured items.

Deliberately two steps. Parsing menus is inexact: prices attach to the wrong
dish, section headers become items, "add bacon +2" is a modifier not a plate.
So a parse produces a preview to correct, and only an explicit commit writes
anything.
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

PROMPT = """Extract menu items from the text below.

Return ONLY a JSON array. No prose, no markdown fences. Each element:
  name          the dish as written
  category      the section it appears under, or your best guess
  price         a number in dollars, or null if none is given
  description   the descriptive line, or null
  tags          any of: vegan, vegetarian, gluten_free, contains_nuts, spicy
  aliases       ONLY shorter or informal ways a caller says it on the phone.
                Never repeat the full name: it is already matched.
                "Grilled Salmon" -> ["salmon", "the salmon"]
                "Nashville Hot Chicken" -> ["hot chicken", "the chicken"]
                "Calamari" -> [] (nothing shorter to say)

Rules:
- Section headers are categories, never items.
- "add X +$2" style lines are modifiers, not items. Skip them.
- If a price is missing, use null rather than guessing.
- Keep names exactly as written. Do not tidy them.
- Leave aliases empty rather than padding them with the name again.

TEXT:
"""


class ParsedItem(BaseModel):
    name: str
    category: str = "Menu"
    price: float | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


def parse_plain(text: str) -> list[ParsedItem]:
    """Rule-based fallback, used when no model key is configured.

    Handles the common shape: a line with a trailing price, under a heading
    that has no price. Crude on purpose, and always reviewed before commit.
    """
    items: list[ParsedItem] = []
    category = "Menu"
    # "add bacon +2.50" is a modifier priced as an extra, not a plate. The
    # leading + or a modifier verb is the giveaway.
    modifier_re = re.compile(
        r"^(add|extra|sub|substitute|side of|make it)\b|\+\s*\$?\d", re.I
    )
    # En and em dashes are deliberate: printed menus use them as leaders
    # between a dish and its price far more often than a hyphen.
    price_re = re.compile(
        r"^(?P<name>.+?)[\s.\-\u2013\u2014]*\$?(?P<price>\d+(?:\.\d{1,2})?)\s*$"
    )

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if modifier_re.search(line):
            continue
        m = price_re.match(line)
        if m:
            name = m.group("name").strip(" .-\u2013\u2014\t")
            if name:
                items.append(
                    ParsedItem(
                        name=name,
                        category=category,
                        price=float(m.group("price")),
                    )
                )
        elif len(line) < 40 and not line.endswith((".", ",")):
            # A short line with no price reads as a section heading.
            category = line.strip(" :-")
    return items


async def parse_with_model(text: str, api_key: str, model: str) -> list[ParsedItem]:
    """Ask the model to structure it. Falls back to rules on any failure,
    because a failed import should still give the user something to correct."""
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model, contents=PROMPT + text[:20000]
        )
        body = (response.text or "").strip()
        body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
        raw = json.loads(body)
        out = []
        for entry in raw:
            try:
                out.append(ParsedItem(**entry))
            except Exception as exc:
                # One malformed entry must not lose the other seventy-nine.
                log.debug("skipping unparseable entry %r: %s", entry, exc)
        if out:
            return out
        log.warning("model returned no usable items; falling back to rules")
    except Exception as exc:
        log.warning("model parse failed (%s); falling back to rules", exc)
    return parse_plain(text)


def clean_aliases(items: list[ParsedItem]) -> list[ParsedItem]:
    """Drop aliases that just repeat the dish name.

    Aliases exist because nobody phones up and asks for "Grilled Salmon";
    they ask for "the salmon". An alias identical to the name matches nothing
    the name would not already match, and it is prompt weight on every call.
    """
    for item in items:
        name = item.name.strip().lower()
        kept: list[str] = []
        seen: set[str] = set()
        for raw in item.aliases:
            alias = raw.strip()
            key = alias.lower()
            # Case-insensitive: "salmon" and "SALMON" match identically at
            # lookup time, so keeping both is pure prompt weight.
            if not alias or key == name or key in seen:
                continue
            if len(alias) >= len(item.name):
                continue
            seen.add(key)
            kept.append(alias.lower())
        item.aliases = kept
    return items


def dedupe(items: list[ParsedItem]) -> list[ParsedItem]:
    """Menus repeat things across sections. Keep the first of each name."""
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out
