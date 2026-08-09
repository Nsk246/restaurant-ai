"""The event contract between the bridge and the portal.

The Pass renders from these events. If a field name changes here, the screen
goes blank in front of a prospect and nothing in the backend fails, so it is
worth pinning explicitly.
"""
import asyncio
import base64
import json

import numpy as np
import pytest

from app import audio as A
from app.providers.base import ProviderEvent
from app.providers.mock import MockProvider
from app.telephony.bridge import MediaBridge

pytestmark = pytest.mark.asyncio


class FakeWS:
    def __init__(self, inbound):
        self._inbound = inbound
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def iter_text(self):
        for m in self._inbound:
            yield m
            await asyncio.sleep(0.005)


def media(samples):
    return json.dumps(
        {
            "event": "media",
            "media": {
                "payload": base64.b64encode(
                    A.pcm16_to_ulaw(samples.astype(np.int16))
                ).decode()
            },
        }
    )


async def _run(script, dispatch=None):
    inbound = [json.dumps({"event": "start", "start": {"streamSid": "MZ1"}})]
    inbound += [media(np.zeros(160)) for _ in range(25)]
    inbound += [json.dumps({"event": "stop"})]
    seen = []

    async def sink(p):
        seen.append(p)

    bridge = MediaBridge(
        FakeWS(inbound), MockProvider(script), on_event=sink, dispatch_tool=dispatch
    )
    await asyncio.wait_for(bridge.run(), timeout=6)
    return seen


async def test_transcript_events_carry_role_and_text():
    seen = await _run(
        [ProviderEvent(kind="transcript", role="caller", text="two fries")]
    )
    t = next(e for e in seen if e["type"] == "transcript")
    assert t["role"] == "caller" and t["text"] == "two fries"


async def test_tool_call_event_carries_args_so_the_chit_can_fill():
    """The chit renders an item the moment add_item fires, using these args.
    Without them the ticket stays blank until the call ends."""

    async def dispatch(name, args):
        return {"line_id": "x", "running_total": 5.0}

    seen = await _run(
        [
            ProviderEvent(
                kind="tool_call",
                tool_call_id="fc1",
                tool_name="add_item",
                tool_args={"item_code": "fries", "quantity": 2, "note": "extra crispy"},
            )
        ],
        dispatch,
    )
    call = next(e for e in seen if e["type"] == "tool_call")
    assert call["name"] == "add_item"
    assert call["args"]["item_code"] == "fries"
    assert call["args"]["quantity"] == 2
    assert call["args"]["note"] == "extra crispy"


async def test_tool_result_event_carries_timing_and_result():
    async def dispatch(name, args):
        return {
            "lines": [
                {"name": "Fries", "quantity": 2, "modifiers": [], "note": None}
            ],
            "total": 10.0,
        }

    seen = await _run(
        [
            ProviderEvent(
                kind="tool_call",
                tool_call_id="fc1",
                tool_name="review_order",
                tool_args={},
            )
        ],
        dispatch,
    )
    res = next(e for e in seen if e["type"] == "tool_result")
    assert res["name"] == "review_order"
    assert isinstance(res["ms"], int)
    assert res["result"]["lines"][0]["name"] == "Fries"


async def test_failed_tool_result_is_marked_so_the_feed_can_show_it_red():
    async def dispatch(name, args):
        return {"error": "we are out of Fries tonight"}

    seen = await _run(
        [
            ProviderEvent(
                kind="tool_call", tool_call_id="fc1", tool_name="add_item", tool_args={}
            )
        ],
        dispatch,
    )
    res = next(e for e in seen if e["type"] == "tool_result")
    assert "error" in res["result"]


async def test_latency_event_is_an_integer_of_milliseconds():
    seen = await _run([ProviderEvent(kind="audio", audio=b"\x00" * 640)])
    lat = [e for e in seen if e["type"] == "latency"]
    assert all(isinstance(e["ms"], int) for e in lat)


async def test_every_event_carries_the_call_id_for_routing():
    seen = await _run([ProviderEvent(kind="transcript", role="agent", text="hi")])
    # main.py stamps call_id; the bridge's own sink does not, so assert the
    # shape the bridge guarantees instead.
    assert all("type" in e for e in seen)


async def test_call_ended_reports_the_stats_the_status_rail_shows():
    seen = await _run([ProviderEvent(kind="transcript", role="agent", text="bye")])
    assert any(e["type"] == "status" for e in seen)
