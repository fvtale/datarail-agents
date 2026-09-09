"""The lead store.

One JSON file holds every lead from both agents. It lives in the datarail-site
repo under public/leads/, which Apache serves behind basic auth, so the same
file is both the agents' database and the dashboard's data source.

A JSON file is a real database at this volume, and it has one property a hosted
CRM does not: the history is in git, so you can see exactly what the agent knew
and when it knew it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from collections.abc import Iterable

SCHEMA_VERSION = 1

STATUSES = ("new", "qualifying", "qualified", "unqualified", "won", "lost")
SOURCES = ("email", "voice")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_email(address: str) -> str:
    """Lowercase and strip an address so the same person matches themselves."""
    return (address or "").strip().lower()


def normalise_phone(number: str) -> str:
    """Reduce a phone number to digits, dropping a US country code.

    The voice agent will get E.164 from the trunk and humans type anything, so
    both have to land on the same key.
    """
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


@dataclass
class Contact:
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""

    def key(self) -> str:
        """Identity used to merge repeat contacts into one lead."""
        if self.email:
            return "email:" + normalise_email(self.email)
        if self.phone:
            return "phone:" + normalise_phone(self.phone)
        return "name:" + self.name.strip().lower()


@dataclass
class Interaction:
    """One inbound message or call, plus whatever the agent sent back."""

    at: str
    source: str
    direction: str  # "in" or "out"
    subject: str = ""
    body: str = ""
    message_id: str = ""


@dataclass
class Lead:
    id: str
    created_at: str
    updated_at: str
    source: str
    contact: Contact

    # What the agent gathered. An empty string means "not established yet",
    # which is different from a client who said they have no budget -- that is
    # recorded as text.
    need: str = ""
    scope: str = ""
    budget: str = ""
    timeline: str = ""
    decision_maker: str = ""

    summary: str = ""
    score: int = 0
    status: str = "new"
    # What the agent still needs answered before this counts as qualified.
    open_questions: list = field(default_factory=list)
    interactions: list = field(default_factory=list)
    thread_ids: list = field(default_factory=list)

    @classmethod
    def new(cls, source: str, contact: Contact) -> Lead:
        stamp = _now()
        return cls(
            id=uuid.uuid4().hex[:12],
            created_at=stamp,
            updated_at=stamp,
            source=source,
            contact=contact,
        )

    def touch(self) -> None:
        self.updated_at = _now()

    def add_interaction(self, interaction: Interaction) -> None:
        self.interactions.append(interaction)
        self.touch()

    def sends_since(self, iso_timestamp: str) -> int:
        """How many outbound messages this lead has had since a given time.

        Backs the per-thread rate limit: an autonomous sender that starts
        looping should run out of allowance before it runs out of patience on
        the other end.
        """
        return sum(
            1
            for item in self.interactions
            if _field(item, "direction") == "out"
            and _field(item, "at", "") >= iso_timestamp
        )


def _field(item, key: str, default=""):
    """Read a field from an Interaction or from the dict it round-trips to."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


class LeadStore:
    """Load, mutate and save the lead file.

    Deliberately not lazy: the whole file is read on open and written on save.
    At the volume a small consultancy generates, the simplicity is worth far
    more than the I/O.
    """

    def __init__(self, path: str):
        self.path = path
        self.leads: list[Lead] = []

    @classmethod
    def open(cls, path: str) -> LeadStore:
        store = cls(path)
        store.load()
        return store

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.leads = []
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.leads = [_lead_from_dict(item) for item in raw.get("leads", [])]

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        ordered = sorted(self.leads, key=lambda item: item.updated_at, reverse=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "count": len(self.leads),
            # Newest first: the dashboard renders in file order.
            "leads": [asdict(lead) for lead in ordered],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, self.path)

    def find_by_contact(self, contact: Contact) -> Lead | None:
        key = contact.key()
        for lead in self.leads:
            if lead.contact.key() == key:
                return lead
        return None

    def find_by_thread(self, thread_id: str) -> Lead | None:
        if not thread_id:
            return None
        for lead in self.leads:
            if thread_id in lead.thread_ids:
                return lead
        return None

    def upsert(self, source: str, contact: Contact, thread_id: str = "") -> Lead:
        """Get the existing lead for this person, or start one.

        Thread wins over contact: a reply in an existing thread belongs to that
        lead even if the person wrote in from a second address.
        """
        lead = self.find_by_thread(thread_id) or self.find_by_contact(contact)
        if lead is None:
            lead = Lead.new(source=source, contact=contact)
            self.leads.append(lead)
        else:
            _merge_contact(lead.contact, contact)
        if thread_id and thread_id not in lead.thread_ids:
            lead.thread_ids.append(thread_id)
        lead.touch()
        return lead

    def __iter__(self) -> Iterable[Lead]:
        return iter(self.leads)

    def __len__(self) -> int:
        return len(self.leads)


def _merge_contact(existing: Contact, incoming: Contact) -> None:
    """Fill gaps in what we know without overwriting what we already had.

    Someone who signs their second email with a full name should upgrade a
    blank name, but a later message with no company must not erase the company
    they gave us the first time.
    """
    for attribute in ("name", "email", "phone", "company"):
        new_value = getattr(incoming, attribute, "").strip()
        if new_value and not getattr(existing, attribute, "").strip():
            setattr(existing, attribute, new_value)


def _lead_from_dict(raw: dict) -> Lead:
    raw_contact = raw.get("contact", {}) or {}
    contact = Contact(
        **{key: raw_contact.get(key, "") for key in ("name", "email", "phone", "company")}
    )
    known = (
        "id", "created_at", "updated_at", "source", "need", "scope", "budget",
        "timeline", "decision_maker", "summary", "score", "status",
        "open_questions", "interactions", "thread_ids",
    )
    fields = {key: raw[key] for key in known if key in raw}
    fields.setdefault("id", uuid.uuid4().hex[:12])
    fields.setdefault("created_at", _now())
    fields.setdefault("updated_at", fields["created_at"])
    fields.setdefault("source", "email")
    return Lead(contact=contact, **fields)
