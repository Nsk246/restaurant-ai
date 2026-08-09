"""FastAPI entrypoint: Twilio webhook, media stream bridge, monitor socket."""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect

from . import db
from .agent import menu as menu_mod
from .agent import prompt as prompt_mod
from .agent import session as session_mod
from .agent.tools import TOOL_SCHEMAS, ToolDispatcher
from .config import get_settings
from .kitchen import InternalKDS
from .providers.gemini import GeminiLiveProvider
from .providers.mock import MockProvider
from .telephony.bridge import MediaBridge
from .telephony.twilio_webhook import connect_stream_twiml, validate_twilio_signature

log = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Suppressed on purpose: the service must still boot without a database so
    # /health can report the outage rather than the container crash-looping.
    with contextlib.suppress(Exception):
        await db.open_pool(settings.database_url)
    yield
    await db.close_pool()


app = FastAPI(title="Restaurant AI Operator", lifespan=lifespan)

# Live monitor sockets, keyed by call id. The portal subscribes here.
_monitors: dict[str, set[WebSocket]] = {}
# Dialled number per call, carried from the webhook to the stream socket.
# Twilio's `start` event does not include it.
_dialled: dict[str, dict] = {}


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
        thinking_level=settings.gemini_thinking_level,
    )


@app.get("/health")
async def health():
    actual = effective_provider()
    body = {"ok": True, "provider": actual}
    if actual != settings.realtime_provider:
        body["configured"] = settings.realtime_provider
        body["note"] = "falling back: GEMINI_API_KEY is unset"
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        body["database"] = "up"
    except Exception:
        body["ok"] = False
        body["database"] = "down"
    return body


@app.post("/twilio/voice")
async def inbound_call(
    request: Request, To: str = Form(""), From: str = Form(""), CallSid: str = Form("")
):
    """Twilio hits this when someone dials."""
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
    # The dialled number is the tenant router, and the stream socket cannot
    # see it, so stash it here.
    _dialled[call_id] = {"to": To, "from": From}

    base = settings.public_base_url.replace("https://", "").replace("http://", "")
    ws_url = f"wss://{base}/ws/twilio/{call_id}"
    return Response(content=connect_stream_twiml(ws_url), media_type="application/xml")


@app.websocket("/ws/twilio/{call_id}")
async def twilio_stream(ws: WebSocket, call_id: str):
    await ws.accept()
    routing = _dialled.pop(call_id, {})

    async def fan_out(payload: dict):
        payload["call_id"] = call_id
        dead = set()
        for sub in _monitors.get(call_id, set()):
            try:
                await sub.send_text(json.dumps(payload))
            except Exception:
                dead.add(sub)
        _monitors.get(call_id, set()).difference_update(dead)

    tenant = None
    conversation_id = None
    dispatcher = None
    instructions = "You answer the phone for a restaurant. Keep replies short."
    tools: list[dict] = []

    try:
        pool = db.pool()
        async with pool.acquire() as conn:
            tenant = await menu_mod.resolve_tenant(conn, routing.get("to", ""))
            if tenant is None:
                log.error("no tenant for dialled number %r", routing.get("to"))
                await ws.close(code=1011)
                return
            snap = await menu_mod.snapshot(conn, tenant.id)
            conversation_id = await session_mod.open_conversation(
                conn,
                restaurant_id=tenant.id,
                channel="phone",
                external_id=call_id,
                from_e164=routing.get("from"),
                recording_disclosed=True,
            )
            await session_mod.upsert_customer(
                conn, restaurant_id=tenant.id, phone_e164=routing.get("from")
            )
        instructions = prompt_mod.build(tenant, snap)
        tools = TOOL_SCHEMAS
        dispatcher = ToolDispatcher(
            pool,
            tenant=tenant,
            menu=snap,
            conversation_id=conversation_id,
            channel="phone",
            kitchen=InternalKDS(notify=fan_out),
        )
    except RuntimeError:
        # No database. The bridge still runs so audio can be exercised, but
        # the agent has no menu and cannot take an order.
        log.warning("no database pool; running without tools")

    bridge = MediaBridge(
        ws,
        build_provider(),
        instructions=instructions,
        tools=tools,
        on_event=fan_out,
        max_call_seconds=settings.max_call_seconds,
        dispatch_tool=dispatcher.dispatch if dispatcher else None,
        tool_timeout_ms=settings.tool_timeout_ms,
    )

    summary, err = {}, None
    try:
        summary = await bridge.run()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        err = str(exc)
        log.exception("call %s failed", call_id)
    finally:
        if conversation_id:
            outcome = "error" if err else _outcome_for(dispatcher, bridge)
            with contextlib.suppress(Exception):
                async with db.pool().acquire() as conn:
                    await session_mod.close_conversation(
                        conn,
                        conversation_id,
                        outcome=outcome,
                        transcript=bridge.transcript,
                        stats=summary,
                        transferred=bool(dispatcher and dispatcher.transfer_requested),
                        error_detail=err,
                    )
        await fan_out({"type": "call_ended", **summary})


def _outcome_for(dispatcher, bridge) -> str:
    if dispatcher is None:
        return "info_only"
    if dispatcher.transfer_requested:
        return "transferred"
    if dispatcher.order_id and any(
        c["name"] == "confirm_order" for c in bridge.tool_calls
    ):
        return "order_placed"
    return "info_only"


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
