"""Handing a live call to a person.

A failed transfer drops someone who has just said "let me speak to a person",
which is the moment they are least likely to call back. So the TwiML is
pinned and the failure path is explicit.
"""
from app.telephony.transfer import TwilioTransfer, dial_twiml

pytest_plugins = ()


def test_dial_twiml_bridges_on_answer():
    """Without answerOnBridge Twilio answers immediately and the caller hears
    silence while the phone is still ringing, which sounds like a dead line."""
    xml = dial_twiml("+16155550100")
    assert 'answerOnBridge="true"' in xml
    assert "<Dial" in xml and "+16155550100" in xml


def test_dial_twiml_carries_the_restaurant_number_as_caller_id():
    """The person picking up should see the restaurant, not a stranger."""
    xml = dial_twiml("+16155550100", caller_id="+15722281712")
    assert 'callerId="+15722281712"' in xml


def test_dial_twiml_can_say_something_first():
    xml = dial_twiml("+16155550100", say="Connecting you now.")
    assert "<Say>Connecting you now.</Say>" in xml
    assert xml.index("<Say>") < xml.index("<Dial")


def test_transfer_without_credentials_is_not_configured():
    assert TwilioTransfer("", "").configured is False
    assert TwilioTransfer("AC123", "token").configured is True


async def test_transfer_without_credentials_reports_failure_not_a_crash():
    """It must return False so the caller can be told, not raise into the
    call teardown where nobody sees it."""
    assert await TwilioTransfer("", "").to_human("CA1", "+16155550100") is False


async def test_transfer_without_a_destination_reports_failure():
    t = TwilioTransfer("AC123", "token")
    assert await t.to_human("CA1", "") is False
