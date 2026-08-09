"""Handing a live call to a person.

The agent answered with <Connect><Stream>, which occupies the call for its
whole duration. You cannot transfer from inside that stream: the only way out
is to tell Twilio, over its REST API, to redirect the call in progress to new
TwiML that dials a human.

Getting this wrong is worse than not offering it. A failed transfer drops
someone who has already said "let me speak to a person", which is the moment
they are least willing to call back.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def dial_twiml(to_e164: str, caller_id: str | None = None, say: str | None = None) -> str:
    """TwiML that rings a person.

    `answerOnBridge` matters: without it Twilio answers immediately and the
    caller hears silence while the phone is still ringing, which sounds like
    the line went dead.
    """
    prefix = f"<Say>{say}</Say>" if say else ""
    attrs = 'answerOnBridge="true" timeout="25"'
    if caller_id:
        attrs += f' callerId="{caller_id}"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{prefix}"
        f"<Dial {attrs}>{to_e164}</Dial>"
        "</Response>"
    )


class TwilioTransfer:
    """Redirects a live call. Never raises into the call path."""

    def __init__(self, account_sid: str, auth_token: str, caller_id: str | None = None):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.caller_id = caller_id

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    async def to_human(self, call_sid: str, to_e164: str, say: str | None = None) -> bool:
        if not self.configured:
            log.warning("transfer requested but Twilio credentials are not set")
            return False
        if not to_e164:
            log.warning("transfer requested but the restaurant has no transfer number")
            return False

        def _redirect() -> None:
            from twilio.rest import Client

            Client(self.account_sid, self.auth_token).calls(call_sid).update(
                twiml=dial_twiml(to_e164, self.caller_id, say)
            )

        try:
            # The Twilio SDK is synchronous; keep it off the loop pumping audio.
            await asyncio.to_thread(_redirect)
            log.info("transferred call %s to %s", call_sid, to_e164)
            return True
        except Exception as exc:
            log.exception("could not transfer call %s: %s", call_sid, exc)
            return False
