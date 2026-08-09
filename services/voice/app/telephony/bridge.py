"""Twilio Media Stream <-> realtime provider bridge.

Design notes, most of them earned by breaking things previously.

Barge-in is one cancellable unit.
    When the caller starts talking over the agent, three things must happen
    together: the provider stops generating, our pending outbound frames are
    dropped, and Twilio's own playback buffer is flushed with a `clear`
    message. Doing only the first two leaves up to a second of already-sent
    audio still playing at the caller's ear, which feels like the agent
    ignoring them.

A grace window guards against line noise.
    Without it, a cough or a burst of background clatter clips the agent
    mid-word. We require sustained caller energy before treating it as a real
    interruption.

Interruption memory is honest.
    When a turn is cut short, the transcript records what actually reached the
    caller, not what the model intended to say. Otherwise the model believes it
    said things the caller never heard and the conversation drifts.

Outbound audio is paced in 20 ms frames.
    Twilio accepts larger writes, but a `clear` can only land between frames.
    Small frames mean interruption latency is bounded by one frame, not by the
    length of whatever blob we last wrote.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import audio as A

# Caller audio above this RMS counts as speech rather than line noise.
log = logging.getLogger(__name__)

BARGE_RMS_THRESHOLD = 550
# Sustained speech required before we treat it as a real interruption.
BARGE_SUSTAIN_FRAMES = 3
# Frames of agent audio to send per pacing tick.
FRAME_MS = 20


@dataclass
class TurnTiming:
    """One turn's latency, for the M1 harness."""

    caller_stopped_at: float | None = None
    agent_first_audio_at: float | None = None

    @property
    def response_ms(self) -> int | None:
        if self.caller_stopped_at is None or self.agent_first_audio_at is None:
            return None
        return int((self.agent_first_audio_at - self.caller_stopped_at) * 1000)


@dataclass
class CallStats:
    turns: list[int] = field(default_factory=list)
    barge_ins: int = 0

    def record(self, ms: int | None) -> None:
        if ms is not None and ms >= 0:
            self.turns.append(ms)

    def percentile(self, p: float) -> int | None:
        if not self.turns:
            return None
        return int(np.percentile(self.turns, p))

    def summary(self) -> dict[str, Any]:
        return {
            "turn_count": len(self.turns),
            "p50_response_ms": self.percentile(50),
            "p95_response_ms": self.percentile(95),
            "barge_ins": self.barge_ins,
        }


class MediaBridge:
    """Pumps audio between one Twilio call and one provider session."""

    def __init__(
        self,
        ws: Any,
        provider: Any,
        *,
        instructions: str = "",
        tools: list[dict] | None = None,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        max_call_seconds: int = 600,
        dispatch_tool: Callable[[str, dict], Awaitable[dict]] | None = None,
        tool_timeout_ms: int = 1200,
        connect_timeout_s: float = 10.0,
    ):
        self.ws = ws
        self.provider = provider
        self.instructions = instructions
        self.tools = tools or []
        self.on_event = on_event
        self.max_call_seconds = max_call_seconds
        self.dispatch_tool = dispatch_tool
        self.tool_timeout_ms = tool_timeout_ms
        self.connect_timeout_s = connect_timeout_s
        self.tool_calls: list[dict] = []
        self._tool_tasks: set[asyncio.Task] = set()

        self.stream_sid: str | None = None
        self.stats = CallStats()
        self.transcript: list[dict] = []

        self._outbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._agent_speaking = False
        self._loud_frames = 0
        self._turn = TurnTiming()
        # Text of the current agent turn that has actually been sent to Twilio.
        self._spoken_this_turn = ""
        self._pending_this_turn = ""
        self._started_at = 0.0
        self._closing = False

    # ---------------------------------------------------------------- helpers

    async def _emit(self, payload: dict) -> None:
        if self.on_event:
            await self.on_event(payload)

    async def _send_to_twilio(self, ulaw: bytes) -> None:
        if not self.stream_sid:
            return
        await self.ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(ulaw).decode()},
                }
            )
        )

    async def _clear_twilio(self) -> None:
        """Flush audio Twilio has buffered but not yet played."""
        if not self.stream_sid:
            return
        await self.ws.send_text(
            json.dumps({"event": "clear", "streamSid": self.stream_sid})
        )

    def _drain_outbound(self) -> None:
        while not self._outbound.empty():
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ------------------------------------------------------------- barge-in

    async def _barge_in(self) -> None:
        """Caller talked over the agent. Stop everything at once."""
        self.stats.barge_ins += 1
        self._drain_outbound()
        await self._clear_twilio()
        await self.provider.interrupt()

        # Record only what the caller actually heard.
        heard = self._spoken_this_turn.strip()
        if heard or self._pending_this_turn.strip():
            self.transcript.append(
                {
                    "role": "agent",
                    "text": heard or "...",
                    "interrupted": True,
                    "unspoken": self._pending_this_turn.strip(),
                    "ts": time.time(),
                }
            )
        self._spoken_this_turn = ""
        self._pending_this_turn = ""
        self._agent_speaking = False
        self._loud_frames = 0
        await self._emit({"type": "barge_in"})

    # ------------------------------------------------------------- pump: in

    async def _phone_to_provider(self) -> None:
        """Twilio receive loop. Ends when the call ends."""
        async for raw in self.ws.iter_text():
            if self._closing:
                break
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                self.stream_sid = msg["start"]["streamSid"]
                self._started_at = time.monotonic()
                await self._emit({"type": "status", "status": "live"})

            elif event == "media":
                payload = base64.b64decode(msg["media"]["payload"])
                samples = A.ulaw_to_pcm16(payload)
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

                if self._agent_speaking:
                    if rms > BARGE_RMS_THRESHOLD:
                        self._loud_frames += 1
                        if self._loud_frames >= BARGE_SUSTAIN_FRAMES:
                            await self._barge_in()
                    else:
                        self._loud_frames = 0
                else:
                    # Caller is talking; the clock for their turn keeps moving.
                    if rms > BARGE_RMS_THRESHOLD:
                        self._turn.caller_stopped_at = None
                    elif self._turn.caller_stopped_at is None:
                        self._turn.caller_stopped_at = time.time()

                await self.provider.send_audio(
                    A.resample(samples, 8000, self.provider.input_hz).tobytes()
                )

            elif event == "stop":
                break

    # ------------------------------------------------------------ pump: out

    async def _provider_to_phone(self) -> None:
        """Provider receive loop.

        The provider adapter guarantees this iterator survives turn
        boundaries. If it ever stops early the bridge goes deaf, which is the
        single worst failure mode this component has.
        """
        async for ev in self.provider.receive():
            if self._closing:
                break

            if ev.kind == "audio":
                if not self._agent_speaking:
                    self._agent_speaking = True
                    if self._turn.agent_first_audio_at is None:
                        self._turn.agent_first_audio_at = time.time()
                        ms = self._turn.response_ms
                        self.stats.record(ms)
                        if ms is not None:
                            await self._emit({"type": "latency", "ms": ms})
                ulaw = A.model_to_phone(ev.audio, self.provider.output_hz)
                chunks, _ = A.frames(ulaw)
                for c in chunks:
                    self._outbound.put_nowait(c)

            elif ev.kind == "transcript":
                self.transcript.append(
                    {"role": ev.role, "text": ev.text, "ts": time.time()}
                )
                if ev.role == "agent":
                    self._pending_this_turn += ev.text
                await self._emit({"type": "transcript", "role": ev.role, "text": ev.text})

            elif ev.kind == "tool_call":
                await self._emit(
                    {"type": "tool_call", "name": ev.tool_name, "args": ev.tool_args}
                )
                self.tool_calls.append({"name": ev.tool_name, "args": ev.tool_args})
                if self.dispatch_tool is not None:
                    # Run it as its own task so a slow tool cannot stall the
                    # audio pump. Silence is what makes an agent feel broken.
                    # The reference is held: an unreferenced task can be
                    # garbage collected mid-flight, which on a live call means
                    # a tool result that silently never arrives.
                    task = asyncio.create_task(self._run_tool(ev))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)

            elif ev.kind == "turn_end":
                self._agent_speaking = False
                self._spoken_this_turn += self._pending_this_turn
                self._pending_this_turn = ""
                self._turn = TurnTiming()
                self._spoken_this_turn = ""
                await self._emit({"type": "turn_end"})

            elif ev.kind == "error":
                await self._emit({"type": "error", "detail": ev.detail})
                break

    async def _run_tool(self, ev) -> None:
        """Execute one tool call and hand the result back to the model.

        Bounded by tool_timeout_ms. A tool that overruns returns a speakable
        message rather than leaving the caller in silence, and the agent can
        stall out loud while the real work finishes.
        """
        started = time.time()
        try:
            result = await asyncio.wait_for(
                self.dispatch_tool(ev.tool_name, ev.tool_args),
                timeout=self.tool_timeout_ms / 1000,
            )
        except TimeoutError:
            result = {"error": "that is taking a moment, tell the caller to hold on"}
            await self._emit({"type": "tool_slow", "name": ev.tool_name})
        except Exception as exc:
            result = {"error": "something went wrong on our end"}
            await self._emit({"type": "error", "detail": str(exc)})

        took = int((time.time() - started) * 1000)
        await self._emit(
            {"type": "tool_result", "name": ev.tool_name, "ms": took, "result": result}
        )
        try:
            await self.provider.send_tool_result(ev.tool_call_id, ev.tool_name, result)
        except Exception as exc:
            await self._emit({"type": "error", "detail": f"tool result: {exc}"})

    async def _pace_outbound(self) -> None:
        """Send queued frames at wall-clock speed so `clear` can interleave."""
        interval = FRAME_MS / 1000
        while not self._closing:
            try:
                frame = await asyncio.wait_for(self._outbound.get(), timeout=0.1)
            except TimeoutError:
                continue
            await self._send_to_twilio(frame)
            self._spoken_this_turn = self._pending_this_turn
            await asyncio.sleep(interval)

    async def _watchdog(self) -> None:
        """Hard cap on call length. A runaway call is a runaway bill."""
        while not self._closing:
            await asyncio.sleep(1)
            if self._started_at and (
                time.monotonic() - self._started_at > self.max_call_seconds
            ):
                await self._emit({"type": "status", "status": "max_duration"})
                self._closing = True
                break

    # ------------------------------------------------------------------ run

    async def run(self) -> dict:
        # A hanging connect is the worst failure mode here: the websocket is
        # open, no exception is raised, the logs look healthy, and the caller
        # hears nothing at all. Bound it so it becomes a visible error.
        try:
            await asyncio.wait_for(
                self.provider.connect(
                    instructions=self.instructions, tools=self.tools
                ),
                timeout=self.connect_timeout_s,
            )
        except TimeoutError:
            log.error(
                "the speech provider did not open a session within %ss; "
                "the caller is hearing silence",
                self.connect_timeout_s,
            )
            await self._emit({"type": "error", "detail": "provider connect timed out"})
            await self.provider.close()
            return self.stats.summary()
        except Exception as exc:
            log.exception("the speech provider failed to open a session")
            await self._emit({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
            await self.provider.close()
            return self.stats.summary()
        log.info("speech session open; bridging call audio")
        tasks = [
            asyncio.create_task(self._provider_to_phone()),
            asyncio.create_task(self._pace_outbound()),
            asyncio.create_task(self._watchdog()),
        ]
        try:
            await self._phone_to_provider()
        finally:
            self._closing = True
            # Let in-flight tools finish briefly so a confirmed order is not
            # abandoned halfway through firing.
            if self._tool_tasks:
                await asyncio.wait(self._tool_tasks, timeout=2)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.provider.close()
        return self.stats.summary()
