"""FastAPI entrypoint: Twilio webhook, media stream bridge, monitor socket."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect

from .config import get_settings
from .providers.gemini import GeminiLiveProvider
from .providers.mock import MockProvider
from .telephony.bridge import MediaBridge
from .telephony.twilio_webhook import connect_stream_twiml, validate_twilio_signature

log = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="Restaurant AI Operator")

# Live monitor sockets, keyed by call id. The portal subscribes here.
_monitors: dict[str, set[WebSocket]] = {}


def effective_provider() -> str:
    """Which provider will actually handle the next call.

    Not the same as the configured value. Asking for gemini with no API key
    used to silently yield a mock while /health still said gemini, which is a
    bad way to lose twenty minutes.
    """
    if settings.realtime_provider == "mock":
        return "mock"
    if settings.realtime_provider == "gemini" and not settings.gemini_api_key:
        if settings.app_env == "prod":
            # Falling back in production means answering real calls with a
            # mock and reporting healthy. Fail loudly instead.
            raise RuntimeError(
                "REALTIME_PROVIDER=gemini but GEMINI_API_KEY is unset. "
                "Set the key or set REALTIME_PROVIDER=mock explicitly."
            )
        log.warning(
            "GEMINI_API_KEY is unset, falling back to the mock provider. "
            "Calls will not reach a real model."
        )
        return "mock"
    return settings.realtime_provider


def build_provider():
    if effective_provider() == "mock":
        return MockProvider()
    return GeminiLiveProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_live_model,
        voice=settings.gemini_voice,
    )


@app.get("/health")
async def health():
    """Reports the provider in use, plus the configured one when they differ."""
    actual = effective_provider()
    body = {"ok": True, "provider": actual}
    if actual != settings.realtime_provider:
        body["configured"] = settings.realtime_provider
        body["note"] = "falling back: GEMINI_API_KEY is unset"
    return body


@app.post("/twilio/voice")
async def inbound_call(request: Request, To: str = Form(""), CallSid: str = Form("")):
    """Twilio hits this when someone dials. Answer with a stream instruction."""
    form = dict(await request.form())
    if settings.twilio_validate_signature and settings.app_env != "test":
        ok = validate_twilio_signature(
            settings.twilio_auth_token,
            str(request.url),
            form,
            request.headers.get("X-Twilio-Signature", ""),
        )
        if not ok:
            return Response(status_code=403, content="invalid signature")

    call_id = CallSid or str(uuid.uuid4())
    base = settings.public_base_url.replace("https://", "").replace("http://", "")
    ws_url = f"wss://{base}/ws/twilio/{call_id}"
    return Response(
        content=connect_stream_twiml(ws_url),
        media_type="application/xml",
    )


@app.websocket("/ws/twilio/{call_id}")
async def twilio_stream(ws: WebSocket, call_id: str):
    await ws.accept()

    async def fan_out(payload: dict):
        payload["call_id"] = call_id
        dead = set()
        for sub in _monitors.get(call_id, set()):
            try:
                await sub.send_text(json.dumps(payload))
            except Exception:
                dead.add(sub)
        _monitors.get(call_id, set()).difference_update(dead)

    bridge = MediaBridge(
        ws,
        build_provider(),
        instructions=(
            "You answer the phone for a restaurant. Keep replies short and "
            "spoken, never more than two sentences. Confirm anything you are "
            "unsure of rather than guessing."
        ),
        on_event=fan_out,
        max_call_seconds=settings.max_call_seconds,
    )
    try:
        summary = await bridge.run()
        await fan_out({"type": "call_ended", **summary})
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/monitor/{call_id}")
async def monitor(ws: WebSocket, call_id: str):
    """The portal listens here for transcript, tool calls, and latency."""
    await ws.accept()
    _monitors.setdefault(call_id, set()).add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _monitors.get(call_id, set()).discard(ws)
