"""Configuration, read from the environment.

Every secret arrives as an environment variable so the same code runs under a
GitHub Actions cron today and on the home box next to the voice agent later.
Nothing here has a default that would let the agent send mail by accident: if a
credential is missing, `Config.load` raises rather than guessing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _env(name: str, default: str = "") -> str:
    """An environment variable, or `default` when it is unset *or empty*.

    GitHub Actions passes an unset repository variable as an empty string, not
    as an absent one. The workflow forwards every optional setting as
    `${{ vars.X }}`, so plain `os.environ.get(name, default)` returned "" and
    never the default -- a blank mailbox address, a blank model name, and
    `int("")` crashing the run -- on exactly the setup the README describes,
    where those variables are optional and left unset.
    """
    value = os.environ.get(name, "").strip()
    return value if value else default


def _require(name: str) -> str:
    value = _env(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. See README.md for the full list of secrets and "
            f"where each one comes from."
        )
    return value


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MailboxConfig:
    """IONOS mailbox for contact@datarail.org.

    IONOS uses implicit TLS on both legs: IMAP on 993, SMTP on 465. The
    hostnames are the same for every IONOS mailbox, so they are defaults rather
    than required secrets.
    """

    address: str
    username: str
    password: str
    imap_host: str = "imap.ionos.com"
    imap_port: int = 993
    smtp_host: str = "smtp.ionos.com"
    smtp_port: int = 465
    # Mail the agent has handled gets moved out of INBOX so a rerun cannot
    # process it twice even if the state file is lost.
    processed_folder: str = "Agent/Handled"
    ignored_folder: str = "Agent/Ignored"
    # Mail the agent wanted to answer but whose draft failed the safety gate.
    # This is the folder actually worth reading: everything in it is a real
    # enquiry still waiting on a human.
    review_folder: str = "Agent/Review"
    # Listings for the Glyph calendar that have been proposed as a pull request.
    # The original is kept here so the reviewer can check the PR against it --
    # the PR itself is public and deliberately carries none of the email.
    listings_folder: str = "Agent/Listings"

    @classmethod
    def load(cls) -> MailboxConfig:
        address = _env("DATARAIL_MAIL_ADDRESS", "contact@datarail.org").strip()
        return cls(
            address=address,
            # IONOS authenticates with the full address as the username.
            username=_env("DATARAIL_MAIL_USERNAME", "").strip() or address,
            password=_require("DATARAIL_MAIL_PASSWORD"),
            imap_host=_env("DATARAIL_IMAP_HOST", "imap.ionos.com").strip(),
            imap_port=int(_env("DATARAIL_IMAP_PORT", "993")),
            smtp_host=_env("DATARAIL_SMTP_HOST", "smtp.ionos.com").strip(),
            smtp_port=int(_env("DATARAIL_SMTP_PORT", "465")),
        )


@dataclass(frozen=True)
class BrainConfig:
    """OpenAI settings.

    DataRail deliberately does not run every system on one vendor, so the
    reasoning here is OpenAI while other DataRail tooling is Claude. `Brain` in
    brain.py is written against a narrow interface for that reason -- swapping
    provider is one class, not a rewrite.
    """

    api_key: str
    model: str = "gpt-5"
    # Classification is a cheap, high-volume call; replies are not. Two models
    # so the cheap half does not pay for the expensive one.
    classifier_model: str = "gpt-5-mini"
    request_timeout: float = 60.0
    max_retries: int = 3

    @classmethod
    def load(cls) -> BrainConfig:
        return cls(
            api_key=_require("OPENAI_API_KEY"),
            model=_env("OPENAI_MODEL", "gpt-5").strip(),
            classifier_model=_env(
                "OPENAI_CLASSIFIER_MODEL", "gpt-5-mini"
            ).strip(),
            request_timeout=float(_env("OPENAI_TIMEOUT", "60")),
        )


@dataclass(frozen=True)
class CalendarConfig:
    """Google Calendar, for booking the free intro call.

    Authenticated as a service account rather than through OAuth, because this
    runs on a cron with nobody present to click a consent screen. The service
    account reaches a personal calendar by being given access to it directly:
    Google Calendar > Settings > the calendar > Share with specific people >
    add the service account's client_email with "Make changes to events".

    Note the service account does NOT invite the client. Google blocks service
    accounts from adding attendees without domain-wide delegation, so the agent
    emails the .ics itself over the SMTP connection it already has -- which also
    means the invite arrives from contact@datarail.org rather than from Google.
    """

    service_account_json: str
    calendar_id: str = "primary"

    # Where the agent thinks it lives. Slots are generated and described in
    # this zone, and every proposal states it, since the client may not share
    # it. 718 and the NYC campaign make Eastern the sensible default.
    timezone: str = "America/New_York"

    slot_minutes: int = 30
    # Gap left after a call, so two bookings cannot land back to back.
    buffer_minutes: int = 15
    # Nothing may be offered sooner than this: a client should not open their
    # mail to a call starting in ten minutes.
    min_notice_hours: int = 12
    # How far ahead to look for free time.
    horizon_days: int = 14
    # How many times to offer at once. Three is enough to feel accommodating
    # and few enough to answer without a calendar app open.
    slots_to_offer: int = 3
    # How long an offer stands before the slots are considered stale and are
    # regenerated against a fresh read of the calendar.
    offer_expiry_hours: int = 72

    # "All hours" -- the calendar's own free/busy is the only constraint, as
    # asked for. Set BOOKING_EARLIEST_HOUR / BOOKING_LATEST_HOUR to narrow it
    # to working hours later; the slot generator reads these and nothing else
    # changes.
    earliest_hour: int = 0
    latest_hour: int = 24

    @classmethod
    def load(cls) -> CalendarConfig:
        return cls(
            service_account_json=_require("GOOGLE_SERVICE_ACCOUNT_JSON"),
            calendar_id=_env("GOOGLE_CALENDAR_ID", "primary").strip(),
            timezone=_env("DATARAIL_TIMEZONE", "America/New_York").strip(),
            slot_minutes=int(_env("BOOKING_SLOT_MINUTES", "30")),
            buffer_minutes=int(_env("BOOKING_BUFFER_MINUTES", "15")),
            min_notice_hours=int(_env("BOOKING_MIN_NOTICE_HOURS", "12")),
            horizon_days=int(_env("BOOKING_HORIZON_DAYS", "14")),
            slots_to_offer=int(_env("BOOKING_SLOTS_TO_OFFER", "3")),
            earliest_hour=int(_env("BOOKING_EARLIEST_HOUR", "0")),
            latest_hour=int(_env("BOOKING_LATEST_HOUR", "24")),
        )


@dataclass(frozen=True)
class Config:
    mailbox: MailboxConfig
    brain: BrainConfig

    # Optional. Without Google credentials the agent still answers and still
    # qualifies -- it simply never offers a time, and says Lynette will follow
    # up with some. Booking is an enhancement, not a dependency.
    calendar: CalendarConfig | None = None

    # Where the lead store and dashboard are written. On Actions this is a
    # checkout of datarail-site; at home it is a working copy of the same repo.
    site_root: str = "site"

    # The master switch. Set DATARAIL_AGENT_DRY_RUN=true and the agent does
    # every bit of its work -- reads mail, classifies, drafts, scores -- but
    # sends nothing and moves nothing. This is the kill switch: flip the repo
    # variable and the next cron run goes quiet without a code change.
    dry_run: bool = True

    # Hard ceiling on outbound mail per run. An autonomous sender that develops
    # a loop should hit a wall long before it hits a mailing list.
    max_sends_per_run: int = 10
    # A single correspondent can only be replied to this many times in a day,
    # however many messages they send.
    max_sends_per_thread_per_day: int = 2
    # Glyph proposals opened per run. Each one is a public pull request, so a
    # burst of junk listing mail should wait in the inbox, not flood the repo.
    max_listings_per_run: int = 5

    operator_name: str = "Lynette"
    operator_email: str = "contact@datarail.org"
    signature_name: str = "DataRail"

    # Whether replies say they were written by an assistant.
    #
    # Defaults to on for three reasons: the EU AI Act requires telling people
    # they are dealing with an AI system, several US states have similar bot
    # disclosure rules, and DataRail's whole pitch is labelling things honestly
    # -- the concept site says so on its own page. A receptionist that hides
    # what it is would be the one dishonest thing on the domain.
    disclose_agent: bool = True
    disclosure_line: str = (
        "This reply was written by DataRail's assistant. "
        "Lynette reads every thread and will pick this one up personally."
    )

    labels: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        return cls(
            mailbox=MailboxConfig.load(),
            brain=BrainConfig.load(),
            # Absent credentials mean booking is off, not that the run fails.
            calendar=(
                CalendarConfig.load()
                if _env("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
                else None
            ),
            site_root=_env("DATARAIL_SITE_ROOT", "site").strip(),
            # Dry run defaults to ON. Sending is something you turn on
            # deliberately, not something you forget to turn off.
            dry_run=_flag("DATARAIL_AGENT_DRY_RUN", True),
            max_sends_per_run=int(_env("DATARAIL_MAX_SENDS_PER_RUN", "10")),
            max_sends_per_thread_per_day=int(
                _env("DATARAIL_MAX_SENDS_PER_THREAD_PER_DAY", "2")
            ),
            max_listings_per_run=int(_env("DATARAIL_MAX_LISTINGS_PER_RUN", "5")),
            disclose_agent=_flag("DATARAIL_DISCLOSE_AGENT", True),
        )
