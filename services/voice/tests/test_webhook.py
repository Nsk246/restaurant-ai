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


class FakeURL:
    def __init__(self, url, scheme, netloc):
        self._url, self.scheme, self.netloc = url, scheme, netloc

    def __str__(self):
        return self._url


class FakeRequest:
    def __init__(self, url, scheme, netloc, headers):
        self.url = FakeURL(url, scheme, netloc)
        self.headers = headers


def test_public_url_rebuilds_the_https_url_twilio_signed():
    """Behind a proxy the app sees http and an internal host. Validating
    against that fails every time and looks like a Twilio problem."""
    from app.telephony.twilio_webhook import public_url

    req = FakeRequest(
        "http://0.0.0.0:8000/twilio/voice",
        "http",
        "0.0.0.0:8000",
        {"x-forwarded-proto": "https", "x-forwarded-host": "abc-8000.app.github.dev"},
    )
    assert public_url(req) == "https://abc-8000.app.github.dev/twilio/voice"


def test_public_url_is_unchanged_without_proxy_headers():
    from app.telephony.twilio_webhook import public_url

    req = FakeRequest(
        "https://voice.example.com/twilio/voice",
        "https",
        "voice.example.com",
        {"host": "voice.example.com"},
    )
    assert public_url(req) == "https://voice.example.com/twilio/voice"


def test_signature_validates_against_the_rebuilt_public_url():
    from app.telephony.twilio_webhook import public_url

    params = {"CallSid": "CA1", "To": "+16155550111"}
    public = "https://abc-8000.app.github.dev/twilio/voice"
    sig = RequestValidator(TOKEN).compute_signature(public, params)

    req = FakeRequest(
        "http://0.0.0.0:8000/twilio/voice",
        "http",
        "0.0.0.0:8000",
        {"x-forwarded-proto": "https", "x-forwarded-host": "abc-8000.app.github.dev"},
    )
    assert validate_twilio_signature(TOKEN, public_url(req), params, sig)
