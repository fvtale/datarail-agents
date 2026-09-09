"""Turning a conversation into a booked intro call.

The flow the /consult page already promises, made real:

  1. A genuine enquiry arrives with no call booked.
  2. The agent reads free/busy from Google Calendar and offers three times,
     recorded on the lead so the next message can be matched against them.
  3. The client replies "the second one works".
  4. The agent re-checks that slot is still free, puts it on the calendar, and
     emails an .ics invitation.

Step 3 is why offers are stored rather than re-derived. "The second one" is
only meaningful against the list that was actually sent, and a list
regenerated a day later is a different list.

Everything here is pure except make_offer and confirm, which touch the
calendar. That keeps the state machine testable without a Google account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .calendar import Calendar, CalendarError, Slot, describe
from .config import CalendarConfig
from .leads import Booking, Lead


class BookingError(RuntimeError):
    """A booking could not be completed. Never fatal to the run."""


@dataclass
class Offer:
    """Times currently on the table, and how they were described."""

    slots: list
    descriptions: list

    def is_empty(self) -> bool:
        return not self.slots

    def numbered(self) -> str:
        """The offer as the model sees it, and as the client will read it."""
        return "\n".join(
            str(index + 1) + ". " + text
            for index, text in enumerate(self.descriptions)
        )


def current_offer(lead: Lead, config: CalendarConfig) -> Offer:
    """The offer standing on this lead, if it is still fresh."""
    booking = lead.booking
    if booking.status != "offered" or booking.is_stale(config.offer_expiry_hours):
        return Offer(slots=[], descriptions=[])

    slots = []
    for raw in booking.offered_slots:
        try:
            slots.append(Slot.from_iso(raw))
        except (KeyError, ValueError):
            # A malformed stored slot means the whole offer is untrustworthy.
            return Offer(slots=[], descriptions=[])

    # Anything that has since gone past is silently dropped, so the agent never
    # re-offers a time that has already happened.
    now = datetime.now(timezone.utc)
    live = [slot for slot in slots if slot.start > now]
    return Offer(
        slots=live,
        descriptions=[describe(slot, config.timezone) for slot in live],
    )


def make_offer(calendar: Calendar, lead: Lead, config: CalendarConfig) -> Offer:
    """Generate and record a fresh set of times.

    Returns an empty offer rather than raising if the calendar is unreachable
    or genuinely full. A receptionist that cannot see the diary should still
    answer the email.
    """
    try:
        free = calendar.free_slots()
    except CalendarError:
        return Offer(slots=[], descriptions=[])

    chosen = _spread(free, config.slots_to_offer)
    if not chosen:
        return Offer(slots=[], descriptions=[])

    lead.booking = Booking(
        status="offered",
        offered_slots=[slot.to_iso() for slot in chosen],
        offered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    lead.touch()

    return Offer(
        slots=chosen,
        descriptions=[describe(slot, config.timezone) for slot in chosen],
    )


def _spread(slots: list, count: int) -> list:
    """Pick times across different days rather than three in one afternoon.

    Offering 9:00, 9:30 and 10:00 on the same Tuesday is technically three
    options and practically one. Taking the earliest slot on each of the next
    few free days gives a client a real choice.
    """
    if not slots:
        return []

    by_day = {}
    for slot in slots:
        key = slot.start.date()
        if key not in by_day:
            by_day[key] = slot

    spread = [by_day[key] for key in sorted(by_day)][:count]

    # If the diary is so full that there are fewer free days than slots wanted,
    # fall back to filling up from whatever is free.
    if len(spread) < count:
        for slot in slots:
            if slot not in spread:
                spread.append(slot)
            if len(spread) == count:
                break

    return sorted(spread, key=lambda item: item.start)[:count]


def confirm(
    calendar: Calendar,
    lead: Lead,
    config: CalendarConfig,
    choice: int,
) -> Slot:
    """Book the slot the client picked. Raises BookingError if it cannot.

    `choice` is 1-based, matching the numbering the client saw.
    """
    offer = current_offer(lead, config)
    if offer.is_empty():
        raise BookingError("no offer is standing on this lead")

    if choice < 1 or choice > len(offer.slots):
        raise BookingError(
            "choice " + str(choice) + " is outside the "
            + str(len(offer.slots)) + " times offered"
        )

    slot = offer.slots[choice - 1]

    if slot.start <= datetime.now(timezone.utc):
        raise BookingError("that time has already passed")

    # The gap between offering and accepting can be days, and the calendar is
    # shared with a human booking their own life into it. This is the check
    # that stops the agent double-booking Lynette.
    if not calendar.is_free(slot):
        raise BookingError("that time is no longer free")

    summary = "DataRail intro call"
    if lead.contact.name:
        summary += " -- " + lead.contact.name
    elif lead.contact.company:
        summary += " -- " + lead.contact.company

    description = _event_description(lead)

    try:
        event = calendar.create_event(
            slot=slot,
            summary=summary,
            description=description,
            client_name=lead.contact.name,
            client_email=lead.contact.email,
        )
    except CalendarError as error:
        raise BookingError("calendar refused the booking: " + str(error)) from error

    lead.booking = Booking(
        status="booked",
        offered_slots=lead.booking.offered_slots,
        offered_at=lead.booking.offered_at,
        slot=slot.to_iso(),
        event_id=str(event.get("id", "")),
        booked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    lead.touch()
    return slot


def _event_description(lead: Lead) -> str:
    """What Lynette sees on the calendar entry.

    The whole point of the qualification is that she opens the event and
    already knows who this is, so everything gathered goes in.
    """
    lines = []
    if lead.contact.company:
        lines.append("Company: " + lead.contact.company)
    if lead.summary:
        lines.append("Summary: " + lead.summary)
    for label, value in (
        ("Need", lead.need),
        ("Scope", lead.scope),
        ("Budget", lead.budget),
        ("Timeline", lead.timeline),
        ("Decision maker", lead.decision_maker),
    ):
        if value:
            lines.append(label + ": " + value)
    lines.append("Lead score: " + str(lead.score) + "/100")
    if lead.open_questions:
        lines.append("")
        lines.append("Still open:")
        lines.extend("  - " + str(item) for item in lead.open_questions)
    return "\n".join(lines)


def invite_text(lead: Lead, slot: Slot, config: CalendarConfig) -> str:
    """The body of the .ics description the client receives."""
    return (
        "Intro call with DataRail"
        + (" for " + lead.contact.company if lead.contact.company else "")
        + ".\n\n"
        + str(config.slot_minutes) + " minutes, no charge. "
        + "We will talk through what you are trying to do and what is in the way.\n\n"
        + "Time: " + describe(slot, config.timezone) + "\n"
        + "Contact: contact@datarail.org / 718 838-9901\n"
        + "More: https://datarail.org/consult"
    )
