"""Turning an email's events into a proposal for the Glyph calendar.

The model reads the email; this module decides what of its reading survives.
Plain code and no model calls, for the same reason policy.py has none: a check
on the model's output must not depend on the model.

Glyph is the final judge -- its listing-intake workflow validates every
proposal against the listing contract before a person sees it. This module's
job is narrower, and it is about privacy as much as correctness. Glyph is a
public repository, and its pull requests are public from the moment they open,
before anyone has approved them. So what reaches it must be the listing and
nothing else: no sender, no message, no free text from the model.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# The fields a listing may carry into Glyph, and how long each may run. Anything
# the model returns outside this list is dropped, however useful it looks.
FIELDS = ("title", "kind", "date", "time", "endTime", "url", "price", "age",
          "accessibility", "description")
LIMITS = {
    "title": 160, "description": 240, "price": 60, "age": 40,
    "accessibility": 160, "url": 500, "kind": 20, "date": 10, "time": 5,
    "endTime": 5,
}

# Contact details, for scrubbing anything headed for a public repository. The
# model is told not to include them; this is what happens when it does anyway.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")

SUBJECT_LIMIT = 100


def scrub(text) -> str:
    """Strip email addresses and phone numbers, and collapse whitespace."""
    cleaned = EMAIL_RE.sub("", str(text or ""))
    cleaned = PHONE_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_web_link(value: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", value or "", re.I))


def host_of(value: str) -> str:
    """The host of a URL, or the domain of an email address, without www."""
    value = (value or "").strip().lower()
    host = value.rsplit("@", 1)[-1] if "@" in value and "://" not in value else (
        urlsplit(value).hostname or ""
    )
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# ids -- kept identical to Glyph's feed/sources/base.py, so a listing proposed
# by email and one entered by hand get the same kind of name
# ---------------------------------------------------------------------------

def slugify(value: str, limit: int = 48) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].rstrip("-") or "untitled"


def make_id(venue_id: str, date: str, title: str) -> str:
    return f"{venue_id}-{date}-{slugify(title, 40)}"


def proposal_ref(message_id: str) -> str:
    """Stable per email, so a retried run pushes the same branch rather than
    opening a second pull request for the same submission."""
    return hashlib.sha256((message_id or "").encode("utf-8")).hexdigest()[:10]


def branch_for(ref: str) -> str:
    return "listings/" + ref


def today_in(tz_name: str) -> str:
    """Today's date where the venues are, which is what 'next Thursday' means."""
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - a bad zone name must not stop the run
        zone = ZoneInfo("America/New_York")
    return datetime.now(zone).date().isoformat()


# ---------------------------------------------------------------------------
# shaping one listing
# ---------------------------------------------------------------------------

def _registration(raw: dict) -> dict:
    """Keep only well-formed registration details. Zero means unknown, not zero."""
    out: dict = {}
    deadline = str(raw.get("deadline") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        out["deadline"] = deadline
    for key in ("sessions", "capacity"):
        try:
            number = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            out[key] = number
    url = str(raw.get("url") or "").strip()
    if is_web_link(url):
        out["url"] = url[:LIMITS["url"]]
    return out


def shape(raw: dict, venues: dict) -> dict:
    """One extracted listing, reduced to the contract's fields and nothing else.

    Deliberately does not repair what it cannot vouch for. An unknown kind stays
    unknown and an unlisted venue stays unlisted, so Glyph's intake flags them
    for a person instead of this module quietly guessing.
    """
    listing: dict = {}
    for field in FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        text = str(value).strip()
        # A URL is left alone: scrubbing would mangle one with an address in its
        # query string. It is checked for being a web link instead.
        if field != "url":
            text = scrub(text)
        text = text[: LIMITS.get(field, 200)].rstrip()
        if text:
            listing[field] = text

    if "kind" in listing:
        listing["kind"] = listing["kind"].lower()
    if "url" in listing and not is_web_link(listing["url"]):
        del listing["url"]

    venue_id = str(raw.get("venueId") or "").strip()
    venue = venues.get(venue_id)
    if venue:
        listing["venueId"] = venue_id
        listing["venue"] = venue["name"]
        if venue.get("neighborhood"):
            listing["neighborhood"] = venue["neighborhood"]
        listing["region"] = venue["region"]
    else:
        # Left empty on purpose: Glyph's intake reports it as a missing venue,
        # and the reviewer either adds the room to the registry or declines.
        listing["venueId"] = ""
        listing["venue"] = scrub(raw.get("venueName") or "")[:120]

    if listing.get("kind") == "workshop" and isinstance(raw.get("registration"), dict):
        registration = _registration(raw["registration"])
        if registration:
            listing["registration"] = registration

    listing["writers"] = []
    listing["id"] = make_id(
        listing["venueId"] or "unlisted-venue",
        listing.get("date") or "undated",
        listing.get("title") or "untitled",
    )
    return listing


# ---------------------------------------------------------------------------
# what the public pull request is allowed to say
# ---------------------------------------------------------------------------

def sender_is_venue(sender: str, venues: list) -> bool | None:
    """Does the sender write from the domain of a venue their listings name?

    A signal for the reviewer, stated as yes or no -- the address itself never
    leaves this process. None when no listed venue is involved.
    """
    domain = host_of(sender)
    sites = {host_of(venue.get("site", "")) for venue in venues if venue}
    sites.discard("")
    if not sites or not domain:
        return None
    return any(domain == site or domain.endswith("." + site) for site in sites)


def commit_message(listings: list, *, received: str, ref: str,
                   sender_matches: bool | None) -> tuple[str, str]:
    """Subject and body for the proposal commit, which become the public PR.

    Built from fixed phrases and the listing fields only. The model's own notes
    are private and go to the run log; nothing here is free text from the model
    or from the email, so nothing here can carry a sender's details.
    """
    venues = sorted({item.get("venue", "") for item in listings if item.get("venue")})
    if len(listings) == 1:
        item = listings[0]
        subject = "Listing: " + (item.get("title") or "untitled")
        if item.get("venue"):
            subject += " — " + item["venue"]
        if item.get("date"):
            subject += ", " + item["date"]
    elif len(venues) == 1:
        subject = str(len(listings)) + " listings from " + venues[0]
    else:
        subject = str(len(listings)) + " listings"
    subject = scrub(subject)
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[: SUBJECT_LIMIT - 1].rstrip() + "…"

    lines = ["Sent in by email, received " + received + ", proposal ref " + ref + "."]
    if sender_matches is True:
        lines.append("The sender writes from the venue's own domain.")
    elif sender_matches is False:
        lines.append(
            "The sender does not write from the venue's domain -- worth confirming "
            "these are theirs to send."
        )

    unlisted = sorted({item["venue"] for item in listings
                       if not item.get("venueId") and item.get("venue")})
    for name in unlisted:
        lines.append(
            "Names a venue Glyph does not list yet: " + name + ". Add it to "
            "public/data/venues.json first, or close this."
        )
    if any(not item.get("venueId") and not item.get("venue") for item in listings):
        lines.append("At least one listing names no venue at all.")

    titles = {item.get("title") for item in listings}
    if len(listings) > 1 and len(titles) == 1:
        lines.append(
            "Expanded from a recurring series in the email: "
            + str(len(listings)) + " dates. Check them against the venue's own page."
        )
    return subject, "\n\n".join(lines)
