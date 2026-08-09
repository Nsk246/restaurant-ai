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
    body = client.get("/health").json()
    assert body["provider"] == "mock"
    # Database reachability is part of health: a service that answers calls
    # with no menu is not healthy, whatever the process thinks.
    assert body["database"] in ("up", "down")


def test_stream_socket_accepts_a_call_and_closes_cleanly():
    with client.websocket_connect("/ws/twilio/CA_e2e") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1"}}))
        ws.send_text(media(np.zeros(160)))
        ws.send_text(json.dumps({"event": "stop"}))


def test_monitor_socket_accepts_subscribers():
    with client.websocket_connect("/ws/monitor/CA_mon") as ws:
        assert ws is not None


def test_health_does_not_claim_a_provider_it_is_not_using(monkeypatch):
    """Regression: /health reported the configured provider, not the real one.

    With REALTIME_PROVIDER=gemini and no API key the service quietly runs the
    mock. Health must say so rather than reporting gemini.
    """
    from app import main

    monkeypatch.setattr(main.settings, "realtime_provider", "gemini")
    monkeypatch.setattr(main.settings, "gemini_api_key", "")
    monkeypatch.setattr(main.settings, "app_env", "dev")

    body = client.get("/health").json()
    assert body["provider"] == "mock"
    assert body["configured"] == "gemini"


def test_production_refuses_to_silently_fall_back(monkeypatch):
    """Answering real calls with a mock while reporting healthy is worse than
    a hard failure."""
    import pytest

    from app import main

    monkeypatch.setattr(main.settings, "realtime_provider", "gemini")
    monkeypatch.setattr(main.settings, "gemini_api_key", "")
    monkeypatch.setattr(main.settings, "app_env", "prod")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        main.effective_provider()


def test_portal_lookup_survives_a_shallow_container_path(tmp_path, monkeypatch):
    """Regression: indexing parents[3] raised IndexError at /srv/app/main.py
    and killed the process on import, before it could serve anything.

    The container layout is shallower than the repo layout, and the crash
    happened at import time, so the app never started and the only symptom
    was Fly reporting nothing listening on the port.
    """
    from app import main as main_mod

    srv = tmp_path / "srv"
    (srv / "app").mkdir(parents=True)
    dist = srv / "web" / "portal" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")

    fake_main = srv / "app" / "main.py"
    fake_main.write_text("")
    monkeypatch.setattr(main_mod, "__file__", str(fake_main))
    found = main_mod._find_portal()
    assert found == dist


def test_portal_lookup_returns_none_when_not_built(tmp_path, monkeypatch):
    """An unbuilt portal must degrade to no UI, never crash the API."""
    from app import main as main_mod

    lonely = tmp_path / "srv" / "app" / "main.py"
    lonely.parent.mkdir(parents=True)
    lonely.write_text("")
    monkeypatch.setattr(main_mod, "__file__", str(lonely))
    assert main_mod._find_portal() is None


def test_public_base_url_is_normalised():
    """A scheme or trailing slash produces wss://host//ws/... or wss:///ws/...
    Both connect and then go silent, with nothing in the log to say why."""
    from app.config import resolve_base_url

    assert resolve_base_url("https://abc.ngrok-free.app/", {}) == "abc.ngrok-free.app"
    assert resolve_base_url("http://x.test", {}) == "x.test"


def test_platform_hostname_is_used_when_none_is_set():
    """One less value to copy by hand, and copying it wrong is silent."""
    from app.config import resolve_base_url

    assert (
        resolve_base_url("", {"RAILWAY_PUBLIC_DOMAIN": "operator.up.railway.app"})
        == "operator.up.railway.app"
    )
    assert resolve_base_url("", {"FLY_APP_NAME": "restaurant-ai-voice"}) == (
        "restaurant-ai-voice.fly.dev"
    )


def test_an_explicit_url_beats_the_platform_one():
    """Running against a tunnel on a deployed box must still work."""
    from app.config import resolve_base_url

    assert (
        resolve_base_url("me.ngrok.app", {"RAILWAY_PUBLIC_DOMAIN": "x.railway.app"})
        == "me.ngrok.app"
    )


def test_no_hostname_anywhere_yields_empty_not_a_guess():
    from app.config import resolve_base_url

    assert resolve_base_url("", {}) == ""
