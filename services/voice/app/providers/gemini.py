"""Gemini Live adapter.

The one thing that matters here is the outer while-loop in `receive()`. The
SDK's async iterator terminates at every turn boundary. Passing that through
to the bridge makes it stop listening after the greeting, and the call then
dies to a keepalive timeout with no obvious cause. The loop below is why that
does not happen.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from .base import ProviderEvent

log = logging.getLogger(__name__)


class GeminiLiveProvider:
    input_hz = 16000
    output_hz = 24000

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = "Aoede",
        thinking_level: str = "minimal",
        end_of_speech_silence_ms: int = 500,
    ):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.thinking_level = thinking_level
        # How long the model waits in silence before deciding the caller has
        # finished. This is added to every single turn, so the default is a
        # large share of perceived latency. Too low and it interrupts people
        # who pause mid-sentence; 400-600ms is the usable range on a phone.
        self.end_of_speech_silence_ms = end_of_speech_silence_ms
        self._session = None
        self._ctx = None
        self._closed = False

    async def connect(self, *, instructions: str, tools: list[dict]) -> None:
        from google import genai  # imported lazily so tests need no SDK

        self._client = genai.Client(api_key=self.api_key)
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": instructions,
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }
        if tools:
            config["tools"] = [{"function_declarations": tools}]
        if self.end_of_speech_silence_ms:
            config["realtime_input_config"] = {
                "automatic_activity_detection": {
                    "silence_duration_ms": self.end_of_speech_silence_ms,
                }
            }
        if self.thinking_level:
            # Lower thinking means faster first audio. On a phone call the
            # caller hears the delay, so this is not a free knob.
            config["thinking_config"] = {"thinking_level": self.thinking_level}

        self._ctx = self._client.aio.live.connect(model=self.model, config=config)
        try:
            self._session = await self._ctx.__aenter__()
        except Exception as exc:
            # Model ids churn on the developer tier. Say which one failed
            # rather than surfacing a bare 404 from deep in the SDK.
            raise RuntimeError(
                f"could not open a Gemini Live session with model "
                f"{self.model!r}. Check the model id is current at "
                f"https://ai.google.dev/gemini-api/docs/models. Underlying "
                f"error: {type(exc).__name__}: {exc}"
            ) from exc

    async def send_audio(self, pcm: bytes) -> None:
        from google.genai import types

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={self.input_hz}")
        )

    async def send_text(self, text: str) -> None:
        await self._session.send_realtime_input(text=text)

    async def send_tool_result(self, call_id: str, name: str, result: dict) -> None:
        from google.genai import types

        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(id=call_id, name=name, response=result)
            ]
        )

    async def interrupt(self) -> None:
        """Gemini Live handles VAD-based interruption server side.

        We still call this so the bridge's contract holds for every provider,
        and so a provider that needs an explicit cancel can implement it.
        """
        return

    async def receive(self) -> AsyncIterator[ProviderEvent]:
        while not self._closed:
            try:
                turn = self._session.receive()
                async for response in turn:
                    sc = getattr(response, "server_content", None)

                    if getattr(response, "data", None):
                        yield ProviderEvent(kind="audio", audio=response.data)

                    if sc is not None:
                        it = getattr(sc, "input_transcription", None)
                        if it is not None and getattr(it, "text", ""):
                            yield ProviderEvent(
                                kind="transcript", role="caller", text=it.text
                            )
                        ot = getattr(sc, "output_transcription", None)
                        if ot is not None and getattr(ot, "text", ""):
                            yield ProviderEvent(
                                kind="transcript", role="agent", text=ot.text
                            )
                        if getattr(sc, "interrupted", False):
                            yield ProviderEvent(kind="turn_end")
                        if getattr(sc, "turn_complete", False):
                            yield ProviderEvent(kind="turn_end")

                    tc = getattr(response, "tool_call", None)
                    if tc is not None:
                        for fc in getattr(tc, "function_calls", []) or []:
                            yield ProviderEvent(
                                kind="tool_call",
                                tool_call_id=getattr(fc, "id", "") or "",
                                tool_name=fc.name,
                                tool_args=dict(fc.args or {}),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield ProviderEvent(kind="error", detail=f"{type(exc).__name__}: {exc}")
                return

    async def close(self) -> None:
        self._closed = True
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception as exc:
                # A failed teardown must never mask the call's real outcome.
                log.warning("gemini session teardown failed: %s", exc)
