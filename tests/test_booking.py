"""Tests for the booking flow.

Nothing here talks to Google. The calendar is faked, which is the point: the
state machine that decides what to offer, what a client's "the second one"
refers to, and when to refuse a booking is where the mistakes would be, and it
should be testable without a service account.
"""

from datetime import datetime, timedelta, timezone

import pytest

from datarail_agents.core import booking as booking_flow
from datarail_agents.core.brain import _as_index
from datarail_agents.core.calendar import Slot, _escape, describe, ics_for
from datarail_agents.core.config import CalendarConfig
from datarail_agents.core.leads import Booking, Contact, Lead


def config(**overrides) -> CalendarConfig:
    defaults = dict(
        service_account_json="{}",
        calendar_id="primary",
        timezone="America/New_York",
        slot_minutes=30,
        buffer_minutes=15,
        min_notice_hours=12,
        horizon_days=14,
        slots_to_offer=3,
        offer_expiry_hours=72,
        earliest_hour=0,
        latest_hour=24,
    )
    defaults.update(overrides)
    return CalendarConfig(**defaults)


def slot_at(days_ahead: float, hour: int = 14) -> Slot:
    start = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return Slot(start=start, end=start + timedelta(minutes=30))


def lead_with(booking: Booking) -> Lead:
    lead = Lead.new("email", Contact(name="Sam Reed", email="sam@example.com"))
    lead.booking = booking
    return lead


class FakeCalendar:
    """Stands in for Google. Records what it was asked to do."""

    def __init__(self, free=True, slots=None, raise_on_create=False):
        self._free = free
        self._slots = slots if slots is not None else []
        self._raise_on_create = raise_on_create
        self.created = []

    def free_slots(self, now=None):
        return list(self._slots)

    def is_free(self, slot):
        return self._free

    def create_event(self, **kwargs):
        if self._raise_on_create:
            from datarail_agents.core.calendar import CalendarError

            raise CalendarError("calendar said no")
        self.created.append(kwargs)
        return {"id": "evt_" + str(len(self.created))}


class TestSlot:
    def test_overlap_is_detected(self):
        slot = slot_at(1)
        assert slot.overlaps(slot.start - timedelta(minutes=10), slot.start + timedelta(minutes=10))

    def test_touching_at_the_boundary_is_not_an_overlap(self):
        # A call ending at 14:00 does not conflict with one starting at 14:00.
        slot = slot_at(1)
        assert not slot.overlaps(slot.end, slot.end + timedelta(hours=1))
        assert not slot.overlaps(slot.start - timedelta(hours=1), slot.start)

    def test_round_trips_through_json(self):
        slot = slot_at(2)
        assert Slot.from_iso(slot.to_iso()) == slot


class TestOfferSpread:
    def test_picks_one_time_per_day(self):
        # Three slots on the same afternoon is one option wearing a disguise.
        slots = [slot_at(1, 9), slot_at(1, 10), slot_at(1, 11), slot_at(2, 9), slot_at(3, 9)]
        picked = booking_flow._spread(slots, 3)
        assert len({slot.start.date() for slot in picked}) == 3

    def test_falls_back_to_filling_up_when_days_are_scarce(self):
        slots = [slot_at(1, 9), slot_at(1, 10)]
        picked = booking_flow._spread(slots, 3)
        assert len(picked) == 2

    def test_empty_in_empty_out(self):
        assert booking_flow._spread([], 3) == []

    def test_results_are_in_time_order(self):
        slots = [slot_at(3, 9), slot_at(1, 9), slot_at(2, 9)]
        picked = booking_flow._spread(slots, 3)
        assert picked == sorted(picked, key=lambda item: item.start)


class TestStandingOffer:
    def test_no_offer_when_nothing_was_proposed(self):
        assert booking_flow.current_offer(lead_with(Booking()), config()).is_empty()

    def test_a_fresh_offer_is_returned(self):
        slots = [slot_at(1), slot_at(2)]
        lead = lead_with(Booking(
            status="offered",
            offered_slots=[slot.to_iso() for slot in slots],
            offered_at=datetime.now(timezone.utc).isoformat(),
        ))
        offer = booking_flow.current_offer(lead, config())
        assert len(offer.slots) == 2

    def test_a_stale_offer_is_discarded(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        lead = lead_with(Booking(
            status="offered",
            offered_slots=[slot_at(1).to_iso()],
            offered_at=old,
        ))
        assert booking_flow.current_offer(lead, config()).is_empty()

    def test_times_that_have_passed_are_dropped(self):
        # Re-offering yesterday is worse than offering nothing.
        lead = lead_with(Booking(
            status="offered",
            offered_slots=[slot_at(-3).to_iso(), slot_at(2).to_iso()],
            offered_at=datetime.now(timezone.utc).isoformat(),
        ))
        offer = booking_flow.current_offer(lead, config())
        assert len(offer.slots) == 1

    def test_a_corrupt_stored_slot_voids_the_whole_offer(self):
        lead = lead_with(Booking(
            status="offered",
            offered_slots=[{"start": "not-a-date", "end": "also-not"}],
            offered_at=datetime.now(timezone.utc).isoformat(),
        ))
        assert booking_flow.current_offer(lead, config()).is_empty()

    def test_numbering_matches_what_the_client_sees(self):
        offer = booking_flow.Offer(slots=[], descriptions=["Monday 9am ET", "Tuesday 2pm ET"])
        assert offer.numbered() == "1. Monday 9am ET\n2. Tuesday 2pm ET"


class TestMakeOffer:
    def test_records_the_offer_on_the_lead(self):
        lead = lead_with(Booking())
        calendar = FakeCalendar(slots=[slot_at(1), slot_at(2), slot_at(3)])
        offer = booking_flow.make_offer(calendar, lead, config())
        assert len(offer.slots) == 3
        assert lead.booking.status == "offered"
        assert len(lead.booking.offered_slots) == 3

    def test_an_empty_diary_result_leaves_the_lead_alone(self):
        lead = lead_with(Booking())
        offer = booking_flow.make_offer(FakeCalendar(slots=[]), lead, config())
        assert offer.is_empty()
        assert lead.booking.status == "none"


class TestConfirm:
    def _offered(self, count=3):
        slots = [slot_at(index + 1) for index in range(count)]
        return lead_with(Booking(
            status="offered",
            offered_slots=[slot.to_iso() for slot in slots],
            offered_at=datetime.now(timezone.utc).isoformat(),
        )), slots

    def test_books_the_chosen_slot(self):
        lead, slots = self._offered()
        calendar = FakeCalendar(free=True)
        booked = booking_flow.confirm(calendar, lead, config(), 2)
        assert booked == slots[1]
        assert lead.booking.status == "booked"
        assert lead.booking.event_id == "evt_1"
        assert len(calendar.created) == 1

    def test_refuses_a_slot_taken_since_it_was_offered(self):
        # The gap between offering and accepting can be days, and a human books
        # their own life into the same calendar.
        lead, _ = self._offered()
        with pytest.raises(booking_flow.BookingError, match="no longer free"):
            booking_flow.confirm(FakeCalendar(free=False), lead, config(), 1)
        assert lead.booking.status == "offered"

    def test_refuses_a_choice_outside_the_offer(self):
        lead, _ = self._offered()
        with pytest.raises(booking_flow.BookingError, match="outside"):
            booking_flow.confirm(FakeCalendar(), lead, config(), 9)

    def test_refuses_when_nothing_was_offered(self):
        with pytest.raises(booking_flow.BookingError, match="no offer"):
            booking_flow.confirm(FakeCalendar(), lead_with(Booking()), config(), 1)

    def test_refuses_a_time_that_has_passed(self):
        lead = lead_with(Booking(
            status="offered",
            offered_slots=[slot_at(-1).to_iso()],
            offered_at=datetime.now(timezone.utc).isoformat(),
        ))
        # The past slot is filtered out of the offer entirely, so the choice
        # lands outside the remaining list rather than booking yesterday.
        with pytest.raises(booking_flow.BookingError):
            booking_flow.confirm(FakeCalendar(), lead, config(), 1)

    def test_a_calendar_failure_leaves_the_lead_unbooked(self):
        lead, _ = self._offered()
        with pytest.raises(booking_flow.BookingError, match="refused"):
            booking_flow.confirm(FakeCalendar(raise_on_create=True), lead, config(), 1)
        assert lead.booking.status == "offered"

    def test_the_event_carries_the_qualification(self):
        # The point of qualifying is that the diary entry already tells you who
        # this is before you dial.
        lead, _ = self._offered()
        lead.contact.company = "Acme"
        lead.need = "a booking site"
        lead.budget = "under 5k"
        lead.score = 80
        calendar = FakeCalendar()
        booking_flow.confirm(calendar, lead, config(), 1)
        description = calendar.created[0]["description"]
        assert "Acme" in description
        assert "a booking site" in description
        assert "under 5k" in description
        assert "80/100" in description


class TestDescribe:
    def test_always_states_a_timezone(self):
        # The agent has no idea where the client is, and "Tuesday at 2pm" with
        # no zone is how meetings get missed.
        text = describe(slot_at(1), "America/New_York")
        assert any(marker in text for marker in ("EST", "EDT", "America/New_York"))

    def test_an_unknown_zone_does_not_crash(self):
        assert describe(slot_at(1), "Not/AZone")


class TestIcs:
    def test_contains_the_parts_a_mail_client_needs(self):
        text = ics_for(
            slot=slot_at(1),
            summary="DataRail intro call",
            description="Thirty minutes, no charge.",
            organiser_email="contact@datarail.org",
            client_email="sam@example.com",
            client_name="Sam Reed",
            tz_name="America/New_York",
        )
        assert "BEGIN:VCALENDAR" in text
        assert "METHOD:REQUEST" in text
        assert "ATTENDEE" in text and "sam@example.com" in text
        assert "ORGANIZER" in text and "contact@datarail.org" in text
        assert text.endswith("\r\n")

    def test_commas_are_escaped(self):
        # An unescaped comma silently truncates the field in RFC 5545.
        assert _escape("Monday, 2pm") == "Monday\\, 2pm"
        assert _escape("a;b") == "a\\;b"
        assert _escape("line\nbreak") == "line\\nbreak"


class TestChosenSlotParsing:
    def test_accepts_the_shapes_models_actually_return(self):
        assert _as_index(2) == 2
        assert _as_index("2") == 2
        assert _as_index(2.0) == 2

    def test_anything_unusable_means_no_choice(self):
        for value in (None, "", "the second one", -1, 0, {}, []):
            assert _as_index(value) == 0, repr(value)
