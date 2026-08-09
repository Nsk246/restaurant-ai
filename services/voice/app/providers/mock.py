"""Scripted provider for tests and offline demos.

Lets the whole bridge, including barge-in and latency accounting, be tested
without an API key, a network, or a phone.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from .base import ProviderEvent


def tone(ms: int, hz: int = 440, rate: int = 16000) -> bytes:
    t = np.linspace(0, ms / 1000, int(rate * ms / 1000), endpoint=False)
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16).tobytes()


class MockProvider:
    input_hz = 16000
    output_hz = 16000

    def __init__(self, script: list[ProviderEvent] | None = None):
        self.sent_audio: list[bytes] = []
        self.sent_text: list[str] = []
        self.interrupts = 0
        self.closed = False
        self.connected = False
        self.instructions = ""
        self.tools: list[dict] = []
        self._queue: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        for ev in script or []:
            self._queue.put_nowait(ev)

    async def connect(self, *, instructions: str, tools: list[dict]) -> None:
        self.connected = True
        self.instructions = instructions
        self.tools = tools

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def interrupt(self) -> None:
        self.interrupts += 1
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def push(self, ev: ProviderEvent) -> None:
        self._queue.put_nowait(ev)

    async def receive(self) -> AsyncIterator[ProviderEvent]:
        while not self.closed:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield ev

    async def close(self) -> None:
        self.closed = True
