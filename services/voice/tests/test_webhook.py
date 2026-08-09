"""Webhook tests. Signature validation is a security control, so it gets tests."""

from twilio.request_validator import RequestValidator

from app.telephony.twilio_webhook import connect_stream_twiml, validate_twilio_signature

TOKEN = "test_auth_token_do_not_use"
URL = "https://example.test/twilio/voice"


def signed(params):
    return RequestValidator(TOKEN).compute_signature(URL, params)


def test_valid_signature_accepted():
    params = {"CallSid": "CA1", "To": "+16155550111"}
    assert validate_twilio_signature(TOKEN, URL, params, signed(params))


def test_tampered_params_rejected():
    params = {"CallSid": "CA1", "To": "+16155550111"}
    sig = signed(params)
    params["To"] = "+19998887777"
    assert not validate_twilio_signature(TOKEN, URL, params, sig)


def test_missing_signature_rejected():
    assert not validate_twilio_signature(TOKEN, URL, {"CallSid": "CA1"}, "")


def test_wrong_token_rejected():
    params = {"CallSid": "CA1"}
    assert not validate_twilio_signature("other_token", URL, params, signed(params))


def test_twiml_uses_connect_not_start():
    """<Start><Stream> is listen-only. Using it makes the caller hear silence."""
    xml = connect_stream_twiml("wss://x.test/ws/twilio/abc")
    assert "<Connect>" in xml and "<Start>" not in xml
    assert 'url="wss://x.test/ws/twilio/abc"' in xml
