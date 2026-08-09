"""Twilio inbound webhook and tenant resolution.

Signature validation is not optional. The stream URL is public, and without
validation anyone who finds it can make your agent talk and burn your minutes.
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator


def public_url(request) -> str:
    """The URL Twilio actually signed.

    Behind a proxy (Codespaces port forwarding, Fly, any load balancer) the
    app sees http:// and an internal host, while Twilio signed the public
    https:// URL. Validating against the internal one fails every time and
    surfaces as a 403 with no explanation.
    """
    url = str(request.url)
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto:
        url = url.replace(f"{request.url.scheme}://", f"{proto}://", 1)
    if host and host != request.url.netloc:
        url = url.replace(request.url.netloc, host, 1)
    return url


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str
) -> bool:
    """Verify the request actually came from Twilio."""
    if not signature:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)


def connect_stream_twiml(ws_url: str, greeting: str | None = None) -> str:
    """TwiML that hands the call to our media stream.

    <Connect><Stream> is bidirectional, unlike <Start><Stream> which is
    listen-only. Getting this wrong means the caller can hear nothing and the
    logs look completely healthy.
    """
    say = f"<Say>{greeting}</Say>" if greeting else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say}"
        f'<Connect><Stream url="{ws_url}" /></Connect>'
        "</Response>"
    )


TENANT_BY_NUMBER_SQL = """
SELECT r.id, r.name, r.timezone, r.agent_config
FROM phone_numbers p
JOIN restaurants r ON r.id = p.restaurant_id
WHERE p.e164 = $1 AND p.is_active AND r.is_active
"""
