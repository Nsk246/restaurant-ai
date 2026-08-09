"""Realtime speech provider interface.

The bridge talks to this, never to a vendor SDK. Two reasons: we benchmark
providers against each other in M1 before committing, and the kiosk in Stage 2
reuses whichever wins without touching adapter code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass
class ProviderEvent:
    """One thing the model did. The bridge only understands these."""

    kind: Literal["audio", "transcript", "tool_call", "turn_end", "error"]
    audio: bytes = b""
    text: str = ""
    role: Literal["caller", "agent", ""] = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    detail: str = ""


class RealtimeProvider(Protocol):
    """A speech-to-speech model session.

    Contract notes learned the hard way:

    * `receive()` must keep yielding across turn boundaries. Several vendor
      SDKs end their async iterator at the end of every model turn. If the
      adapter passes that termination through, the bridge goes deaf after the
      greeting and the call dies to a keepalive timeout. Adapters wrap the
      vendor iterator in an outer loop so this generator only ends when the
      session actually closes.

    * `interrupt()` must be safe to call when the model is not speaking.
    """

    input_hz: int
    output_hz: int

    async def connect(self, *, instructions: str, tools: list[dict]) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def send_tool_result(self, call_id: str, name: str, result: dict) -> None: ...
    async def interrupt(self) -> None: ...
    def receive(self) -> AsyncIterator[ProviderEvent]: ...
    async def close(self) -> None: ...
