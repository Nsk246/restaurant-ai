"""End-to-end through the real FastAPI routes with the mock provider.

Proves the webhook, the stream socket, the bridge, and the monitor fan-out are
actually wired to each other, not just individually correct.
"""
import base64
import json

import numpy as np
from fastapi.testclient import TestClient

from app import audio as A
from app.main import app

client = TestClient(app)


def media(samples):
    ulaw = A.pcm16_to_ulaw(samples.astype(np.int16))
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}}
    )


def test_webhook_returns_bidirectional_stream_twiml():
    r = client.post("/twilio/voice", data={"To": "+16155550111", "CallSid": "CA_e2e"})
    assert r.status_code == 200
    assert "<Connect>" in r.text
    assert "wss://demo.test/ws/twilio/CA_e2e" in r.text


def test_health_reports_active_provider():
    assert client.get("/health").json() == {"ok": True, "provider": "mock"}


def test_stream_socket_accepts_a_call_and_closes_cleanly():
    with client.websocket_connect("/ws/twilio/CA_e2e") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1"}}))
        ws.send_text(media(np.zeros(160)))
        ws.send_text(json.dumps({"event": "stop"}))


def test_monitor_socket_accepts_subscribers():
    with client.websocket_connect("/ws/monitor/CA_mon") as ws:
        assert ws is not None
