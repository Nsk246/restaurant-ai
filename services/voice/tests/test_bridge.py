"""Bridge behaviour tests, driven entirely by the mock provider.

No network, no API key, no phone. These are the tests that have to hold for
the demo not to embarrass anyone.
"""

import asyncio
import base64
import json

import numpy as np
import pytest

from app import audio as A
from app.providers.base import ProviderEvent
from app.providers.mock import MockProvider, tone
from app.telephony.bridge import BARGE_SUSTAIN_FRAMES, MediaBridge


class FakeTwilioWS:
    """Stands in for Twilio's WebSocket. Records everything we send it."""

    def __init__(self, inbound):
        self._inbound = inbound
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def iter_text(self):
        for msg in self._inbound:
            yield msg
            await asyncio.sleep(0.005)

    def __aiter__(self):
        return self.iter_text()

    def events(self, kind):
        return [m for m in self.sent if m.get("event") == kind]


def start_msg(sid="MZ123"):
    return json.dumps({"event": "start", "start": {"streamSid": sid}})


def media_msg(samples):
    ulaw = A.pcm16_to_ulaw(samples.astype(np.int16))
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}}
    )


def loud(n=160):
    return (np.random.randn(n) * 6000).astype(np.int16)


def quiet(n=160):
    return np.zeros(n, dtype=np.int16)


@pytest.mark.asyncio
async def test_agent_audio_reaches_twilio_as_ulaw_frames():
    ws = FakeTwilioWS([start_msg(), media_msg(quiet()), json.dumps({"event": "stop"})])
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(100))])
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)

    media = ws.events("media")
    assert media, "no audio was sent to Twilio"
    payload = base64.b64decode(media[0]["media"]["payload"])
    assert len(payload) == 160, "frames must be 20ms of 8kHz mu-law"


@pytest.mark.asyncio
async def test_receive_loop_survives_turn_boundaries():
    """The deafness regression: a turn_end must not end the receive loop."""
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(quiet()) for _ in range(20)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(kind="audio", audio=tone(40)),
            ProviderEvent(kind="turn_end"),
            ProviderEvent(kind="audio", audio=tone(40)),
            ProviderEvent(kind="turn_end"),
            ProviderEvent(kind="audio", audio=tone(40)),
        ]
    )
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)
    # Audio from after the first turn boundary must still have gone out.
    assert len(ws.events("media")) >= 4


@pytest.mark.asyncio
async def test_sustained_speech_triggers_barge_in_and_clears_twilio():
    inbound = [start_msg()]
    inbound += [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
    inbound += [json.dumps({"event": "stop"})]
    ws = FakeTwilioWS(inbound)
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(2000))])
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert bridge.stats.barge_ins == 1
    assert ws.events("clear"), "Twilio buffer was never flushed"
    assert provider.interrupts == 1


@pytest.mark.asyncio
async def test_single_noise_burst_does_not_trigger_barge_in():
    """A cough must not clip the agent mid-word."""
    inbound = [start_msg(), media_msg(loud()), media_msg(quiet()), media_msg(quiet())]
    inbound += [json.dumps({"event": "stop"})]
    ws = FakeTwilioWS(inbound)
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(2000))])
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert bridge.stats.barge_ins == 0
    assert not ws.events("clear")


@pytest.mark.asyncio
async def test_interrupted_turn_records_only_what_was_heard():
    inbound = [start_msg()]
    inbound += [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
    inbound += [json.dumps({"event": "stop"})]
    ws = FakeTwilioWS(inbound)
    provider = MockProvider(
        [
            ProviderEvent(kind="transcript", role="agent", text="Your total comes to"),
            ProviderEvent(kind="audio", audio=tone(2000)),
        ]
    )
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)

    cut = [t for t in bridge.transcript if t.get("interrupted")]
    assert cut, "interrupted agent turn was not recorded"
    assert "unspoken" in cut[0]


@pytest.mark.asyncio
async def test_latency_is_measured_per_turn():
    inbound = [start_msg(), media_msg(loud())]
    inbound += [media_msg(quiet()) for _ in range(30)]
    inbound += [json.dumps({"event": "stop"})]
    ws = FakeTwilioWS(inbound)
    provider = MockProvider()
    bridge = MediaBridge(ws, provider)

    async def delayed():
        await asyncio.sleep(0.05)
        provider.push(ProviderEvent(kind="audio", audio=tone(40)))

    task = asyncio.create_task(delayed())
    summary = await asyncio.wait_for(bridge.run(), timeout=5)
    await task
    assert summary["turn_count"] >= 1
    assert summary["p50_response_ms"] is not None


@pytest.mark.asyncio
async def test_events_are_emitted_for_the_portal():
    seen = []

    async def sink(payload):
        seen.append(payload)

    ws = FakeTwilioWS([start_msg(), media_msg(quiet()), json.dumps({"event": "stop"})])
    provider = MockProvider(
        [
            ProviderEvent(kind="transcript", role="caller", text="two hot chicken"),
            ProviderEvent(kind="tool_call", tool_name="add_item", tool_args={"qty": 2}),
        ]
    )
    bridge = MediaBridge(ws, provider, on_event=sink)
    await asyncio.wait_for(bridge.run(), timeout=5)

    kinds = {e["type"] for e in seen}
    assert {"status", "transcript", "tool_call"} <= kinds


@pytest.mark.asyncio
async def test_provider_receives_instructions_and_tools():
    ws = FakeTwilioWS([start_msg(), json.dumps({"event": "stop"})])
    provider = MockProvider()
    bridge = MediaBridge(
        ws, provider, instructions="be brief", tools=[{"name": "add_item"}]
    )
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert provider.connected
    assert provider.instructions == "be brief"
    assert provider.tools == [{"name": "add_item"}]


@pytest.mark.asyncio
async def test_caller_audio_is_resampled_to_provider_rate():
    ws = FakeTwilioWS([start_msg(), media_msg(quiet(160)), json.dumps({"event": "stop"})])
    provider = MockProvider()
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert provider.sent_audio
    got = np.frombuffer(provider.sent_audio[0], dtype=np.int16)
    assert len(got) == 320, "160 samples at 8kHz must become 320 at 16kHz"


@pytest.mark.asyncio
async def test_tool_calls_are_executed_and_results_returned_to_the_model():
    calls = []

    async def dispatch(name, args):
        calls.append((name, args))
        return {"order_number": 7}

    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(quiet()) for _ in range(20)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(
                kind="tool_call",
                tool_call_id="fc1",
                tool_name="add_item",
                tool_args={"quantity": 2},
            )
        ]
    )
    bridge = MediaBridge(ws, provider, dispatch_tool=dispatch)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert calls == [("add_item", {"quantity": 2})]
    assert provider.tool_results
    assert provider.tool_results[0]["result"] == {"order_number": 7}


@pytest.mark.asyncio
async def test_a_slow_tool_does_not_leave_the_caller_in_silence():
    async def slow(name, args):
        await asyncio.sleep(2)
        return {"never": "arrives"}

    seen = []

    async def sink(p):
        seen.append(p)

    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(quiet()) for _ in range(25)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(
                kind="tool_call", tool_call_id="fc1", tool_name="quote", tool_args={}
            )
        ]
    )
    bridge = MediaBridge(
        ws, provider, dispatch_tool=slow, tool_timeout_ms=100, on_event=sink
    )
    await asyncio.wait_for(bridge.run(), timeout=6)

    assert any(e["type"] == "tool_slow" for e in seen)
    assert provider.tool_results, "model was never told anything"
    assert "error" in provider.tool_results[0]["result"]


@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_not_crashed():
    async def boom(name, args):
        raise RuntimeError("database on fire")

    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(quiet()) for _ in range(20)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(
                kind="tool_call", tool_call_id="fc1", tool_name="quote", tool_args={}
            )
        ]
    )
    bridge = MediaBridge(ws, provider, dispatch_tool=boom)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert "error" in provider.tool_results[0]["result"]


@pytest.mark.asyncio
async def test_a_hanging_provider_connect_becomes_an_error_not_silence():
    """The worst failure mode: socket open, no exception, healthy logs, and a
    caller hearing nothing. It must surface as an error the portal can show."""

    class HangingProvider(MockProvider):
        async def connect(self, *, instructions, tools):
            await asyncio.sleep(30)

    seen = []

    async def sink(p):
        seen.append(p)

    ws = FakeTwilioWS([start_msg(), json.dumps({"event": "stop"})])
    bridge = MediaBridge(
        ws, HangingProvider(), on_event=sink, connect_timeout_s=0.2
    )
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert any(
        e["type"] == "error" and "connect" in e.get("detail", "") for e in seen
    ), seen


@pytest.mark.asyncio
async def test_a_failing_provider_connect_is_reported():
    class BrokenProvider(MockProvider):
        async def connect(self, *, instructions, tools):
            raise RuntimeError("model not found")

    seen = []

    async def sink(p):
        seen.append(p)

    ws = FakeTwilioWS([start_msg(), json.dumps({"event": "stop"})])
    bridge = MediaBridge(ws, BrokenProvider(), on_event=sink)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert any("model not found" in str(e.get("detail", "")) for e in seen), seen


@pytest.mark.asyncio
async def test_the_agent_is_nudged_to_speak_first_on_an_inbound_call():
    """Without this both sides wait for the other and the caller hears dead
    air, which sounds like a broken line rather than a silent agent."""
    ws = FakeTwilioWS(
        [start_msg()] + [media_msg(quiet()) for _ in range(5)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider()
    bridge = MediaBridge(ws, provider, greeting="(greet the caller now)")
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert provider.sent_text == ["(greet the caller now)"]


@pytest.mark.asyncio
async def test_no_greeting_means_no_nudge():
    ws = FakeTwilioWS([start_msg(), json.dumps({"event": "stop"})])
    provider = MockProvider()
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert provider.sent_text == []


@pytest.mark.asyncio
async def test_a_slow_tool_makes_the_agent_speak_rather_than_go_quiet():
    """Dead air is the single thing that makes a voice agent feel broken."""

    async def slowish(name, args):
        await asyncio.sleep(0.3)
        return {"ok": True}

    seen = []

    async def sink(p):
        seen.append(p)

    ws = FakeTwilioWS(
        [start_msg()] + [media_msg(quiet()) for _ in range(30)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(
                kind="tool_call",
                tool_call_id="fc1",
                tool_name="quote",
                tool_args={},
            )
        ]
    )
    bridge = MediaBridge(
        ws, provider, dispatch_tool=slowish, stall_after_ms=50, on_event=sink
    )
    await asyncio.wait_for(bridge.run(), timeout=6)

    assert any(e["type"] == "stalling" for e in seen), seen
    assert any("taking a second" in s for s in provider.sent_text), provider.sent_text
    # And the real answer still arrives, rather than being abandoned.
    assert provider.tool_results[0]["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_a_fast_tool_produces_no_filler():
    """Filling silence that is not there would make it sound hesitant."""

    async def quick(name, args):
        return {"ok": True}

    seen = []

    async def sink(p):
        seen.append(p)

    ws = FakeTwilioWS(
        [start_msg()] + [media_msg(quiet()) for _ in range(20)]
        + [json.dumps({"event": "stop"})]
    )
    provider = MockProvider(
        [
            ProviderEvent(
                kind="tool_call",
                tool_call_id="fc1",
                tool_name="quote",
                tool_args={},
            )
        ]
    )
    bridge = MediaBridge(ws, provider, dispatch_tool=quick, on_event=sink)
    await asyncio.wait_for(bridge.run(), timeout=5)

    assert not any(e["type"] == "stalling" for e in seen)
    assert provider.sent_text == []
