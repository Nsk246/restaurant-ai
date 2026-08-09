"""Which calls are actually connected right now.

Liveness was previously inferred from `conversations.ended_at IS NULL`, which
is wrong twice over: a crashed process leaves a row open forever, and a row
created outside a real call looks live even though nobody is on the line.

The server already knows the truth, because it holds the websocket. This is
that truth, in one place both the call handler and the API can see.
"""
from __future__ import annotations

_active: dict[str, str] = {}  # call_id -> restaurant_id


def mark_live(call_id: str, restaurant_id: str) -> None:
    _active[call_id] = restaurant_id


def mark_ended(call_id: str) -> None:
    _active.pop(call_id, None)


def live_call_ids(restaurant_id: str | None = None) -> set[str]:
    if restaurant_id is None:
        return set(_active)
    return {c for c, r in _active.items() if r == restaurant_id}


def clear() -> None:
    """Tests and demo reset."""
    _active.clear()
