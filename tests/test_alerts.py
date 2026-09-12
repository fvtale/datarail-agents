"""Tests for the copy of each reply that goes to the operator.

The alert exists so a reply sent with nobody watching is still seen by someone.
It goes to a personal address, so what it carries and where it is allowed to go
are both worth pinning down.
"""

import pytest

from datarail_agents.core.config import Config
from datarail_agents.email_agent.mailbox import InboundMessage
from datarail_agents.email_agent.run import alert_text


def message(**overrides) -> InboundMessage:
    base = {
        "uid": "1", "message_id": "<abc@mail.example>", "sender_name": "Sam Reed",
        "sender_email": "sam@example.com", "subject": "Booking form",
        "body": "Can you build one?", "date": "Thu, 11 Sep 2026 12:00:00 +0000",
    }
    base.update(overrides)
    return InboundMessage(**base)


class TestAlertText:
    def test_it_carries_who_wrote_and_what_was_sent(self):
        text = alert_text(
            message=message(), subject="Re: Booking form",
            body="Happy to help.\n\nDataRail", booked=False, score=40,
        )
        assert "Sam Reed <sam@example.com>" in text
        assert "Booking form" in text
        assert "Re: Booking form" in text
        assert "Happy to help." in text
        assert "40 out of 100" in text

    def test_it_opens_by_saying_nothing_is_needed(self):
        # An alert that reads like a task becomes one, and ten of those a day
        # is how a person starts ignoring them.
        text = alert_text(message=message(), subject="Re: x", body="y",
                          booked=False, score=0)
        opening = "\n".join(text.splitlines()[:2])
        assert "Nothing is needed from you" in opening

    def test_a_booking_is_called_out(self):
        booked = alert_text(message=message(), subject="Re: x", body="y",
                            booked=True, score=80)
        assert "intro call confirmed" in booked
        assert "intro call confirmed" not in alert_text(
            message=message(), subject="Re: x", body="y", booked=False, score=80
        )

    def test_a_sender_with_no_name_still_reads_properly(self):
        text = alert_text(message=message(sender_name=""), subject="Re: x",
                          body="y", booked=False, score=0)
        assert "From:    sam@example.com" in text

    def test_a_message_with_no_subject_is_not_a_blank(self):
        text = alert_text(message=message(subject=""), subject="Re: x", body="y",
                          booked=False, score=0)
        assert "(no subject)" in text


class TestAlertConfig:
    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("DATARAIL_MAIL_PASSWORD", "secret")
        return monkeypatch

    def test_alerts_are_off_unless_an_address_is_set(self, env):
        env.setenv("DATARAIL_ALERT_ADDRESS", "")
        assert Config.load().alert_address == ""

    def test_the_address_is_read_from_the_environment(self, env):
        env.setenv("DATARAIL_ALERT_ADDRESS", "someone@example.com")
        assert Config.load().alert_address == "someone@example.com"
