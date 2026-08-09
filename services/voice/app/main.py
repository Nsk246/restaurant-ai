"""FastAPI entrypoint: Twilio webhook, media stream bridge, monitor socket."""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, live, notify
from .agent import menu as menu_mod
from .agent import prompt as prompt_mod
from .agent import session as session_mod
from .agent.tools import TOOL_SCHEMAS, ToolDispatcher
from .api import broadcast_rail
from .api import router as api_router
from .config import get_settings, log_config_source
from .kitchen import InternalKDS
from .menu_admin import router as menu_router
from .providers.gemini import GeminiLiveProvider
from .providers.mock import MockProvider
from .telephony.bridge import MediaBridge
from .telephony.transfer import TwilioTransfer
from .telephony.twilio_webhook import (
    connect_stream_twiml,
    public_url,
    validate_twilio_signature,
)

log = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    log.info("config from %s", log_config_source())
    log.info(
        "provider=%s  public_base_url=%r  signature_validation=%s",
        settings.realtime_provider,
        settings.public_base_url,
        settings.twilio_validate_signature,
    )
    if not settings.public_base_url:
        log.error(
            "PUBLIC_BASE_URL is empty. The stream URL handed to Twilio will "
            "be wss:///ws/twilio/... with no host, and the call will connect "
            "and then go silent."
        )
    # Suppressed on purpose: the service must still boot without a database so
    # /health can report the outage rather than the container crash-looping.
    with contextlib.suppress(Exception):
        await db.open_pool(settings.database_url)
    yield
    await db.close_pool()


app = FastAPI(title="Restaurant AI Operator", lifespan=lifespan)
app.include_router(api_router)
app.include_router(menu_router)


def build_sms() -> notify.SmsSender:
    return notify.SmsSender(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_from_number,
        demo_mode=settings.demo_mode,
        allowlist=settings.demo_sms_allowlist.split(","),
    )

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
        end_of_speech_silence_ms=settings.gemini_end_of_speech_ms,
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
        signed_url = public_url(request)
        ok = validate_twilio_signature(
            settings.twilio_auth_token,
            signed_url,
            form,
            request.headers.get("X-Twilio-Signature", ""),
        )
        if not ok:
            log.warning(
                "twilio signature mismatch. Validated against %s. If that is not "
                "the URL configured in the Twilio console, they must match "
                "exactly, including https and any trailing slash.",
                signed_url,
            )
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
        live.mark_live(call_id, tenant.id)
        # Tell any open portal a call has started, so it can attach its
        # monitor socket immediately instead of polling to find out.
        await broadcast_rail(
            tenant.id,
            {
                "type": "call_started",
                "call_id": call_id,
                "from": routing.get("from"),
            },
        )
        instructions = prompt_mod.build(tenant, snap)
        tools = TOOL_SCHEMAS
        dispatcher = ToolDispatcher(
            pool,
            tenant=tenant,
            menu=snap,
            conversation_id=conversation_id,
            channel="phone",
            kitchen=InternalKDS(notify=_on_ticket(tenant.id, fan_out)),
            max_clarify_attempts=settings.max_clarify_attempts,
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
        greeting=(
            f"(The call has just connected. Greet the caller now, in one short "
            f"sentence, as {tenant.name}.)"
            if tenant is not None
            else None
        ),
    )

    summary, err = {}, None
    try:
        summary = await bridge.run()

        # The agent asked for a person. Redirect the live call now that the
        # media stream has ended, which is the only point Twilio will accept
        # a redirect for a call it is still bridging.
        if dispatcher is not None and dispatcher.transfer_requested:
            transfer = TwilioTransfer(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                caller_id=routing.get("to"),
            )
            handed_over = await transfer.to_human(
                call_id,
                tenant.transfer_phone if tenant else "",
                say="Connecting you now.",
            )
            await fan_out({"type": "transfer", "ok": handed_over})
            if not handed_over:
                log.error(
                    "transfer failed for call %s; the caller was dropped after "
                    "asking for a person",
                    call_id,
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        err = str(exc)
        log.exception("call %s failed", call_id)
    finally:
        live.mark_ended(call_id)
        if tenant is not None:
            with contextlib.suppress(Exception):
                await broadcast_rail(
                    tenant.id, {"type": "call_finished", "call_id": call_id}
                )
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


def _on_ticket(restaurant_id: str, fan_out):
    """A fired ticket goes three places: the call view, the kitchen rail, and
    the customer's phone. None of them may break the other two."""

    async def handle(payload: dict):
        await fan_out(payload)
        await broadcast_rail(restaurant_id, payload)
        if payload.get("type") != "ticket":
            return
        try:
            async with db.pool().acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT o.order_number, o.total_cents, o.quoted_minutes,
                           o.customer_id, c.phone_e164, r.name
                    FROM orders o
                    JOIN restaurants r ON r.id = o.restaurant_id
                    LEFT JOIN customers c ON c.id = o.customer_id
                    WHERE o.id = $1
                    """,
                    payload["order_id"],
                )
                if row is None or not row["phone_e164"]:
                    return
                queued = await notify.queue(
                    conn,
                    restaurant_id=restaurant_id,
                    to_e164=row["phone_e164"],
                    body=notify.confirmation_body(
                        row["name"],
                        row["order_number"],
                        row["total_cents"] or 0,
                        row["quoted_minutes"],
                    ),
                    template="order_confirmation",
                    idempotency_key=f"order:{payload['order_id']}:confirmation",
                    order_id=payload["order_id"],
                    customer_id=str(row["customer_id"]) if row["customer_id"] else None,
                )
                if queued:
                    await build_sms().send_pending(conn, restaurant_id)
        except Exception as exc:
            # A text that fails must never fail an order the kitchen is
            # already cooking.
            log.warning("confirmation sms not sent: %s", exc)

    return handle


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


# Serve the built portal from the same origin as the API. One process for a
# demo means one thing to start and no CORS surprises on a hotel network.
#
# Assets get a real mount; everything else falls back to index.html through a
# plain GET route. Mounting StaticFiles at "/" would swallow the websocket
# routes, because a mount matches on path prefix regardless of scope type.
def _find_portal() -> pathlib.Path | None:
    """Locate the built portal in both the source tree and the container.

    In the repo it sits three levels above app/; in the image the app is at
    /srv/app and the portal at /srv/web/portal/dist. Walk the ancestors
    rather than indexing a fixed depth: /srv/app/main.py has only three
    parents, so parents[3] raises IndexError and the process dies on import
    before it can serve anything.
    """
    here = pathlib.Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "web" / "portal" / "dist"
        if (candidate / "index.html").is_file():
            return candidate
    return None


_portal = _find_portal()
if _portal is not None:
    app.mount("/assets", StaticFiles(directory=str(_portal / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def portal(path: str = ""):
        if path.startswith(("api/", "ws/", "twilio/", "health")):
            raise HTTPException(404, "not found")
        return FileResponse(_portal / "index.html")

else:
    log.info("portal not built; run: make portal")


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
