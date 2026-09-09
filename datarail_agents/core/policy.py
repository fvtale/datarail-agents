"""What the autonomous agents are and are not allowed to do.

The agents send without a human in the loop, so this module is the thing
standing between a bad model turn and a real client. It is deliberately dumb:
plain rules and regexes, no model calls. A guardrail that needs the model to be
working is not a guardrail.

Two gates:

  should_reply()   -- decided before the model is asked to write anything
  vet_reply()      -- decided after, on the text the model actually produced

Anything that fails either gate is recorded on the lead and left for a human.
Nothing fails silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class Decision:
    """The outcome of a gate, with the reason it came out that way.

    The reason is written to the lead and the run log. When the agent stays
    quiet you should never have to guess why.
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


# --------------------------------------------------------------------------
# Gate 1: should this message get a reply at all?
# --------------------------------------------------------------------------

# Local-parts that are machines. Replying to these achieves nothing at best and
# starts a loop at worst.
MACHINE_LOCAL_PARTS = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces", "notifications",
    "notification", "alerts", "alert", "automated", "auto-confirm",
    "support@github.com",
)

# Headers that mark bulk or automated mail. RFC 3834 says an auto-responder
# must not reply to anything carrying these, and that is the exact failure mode
# that produces mail loops.
AUTOMATION_HEADERS = (
    "list-unsubscribe",
    "list-id",
    "precedence",
    "auto-submitted",
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
)

AUTO_SUBMITTED_HUMAN_VALUES = ("no",)


def is_machine_sender(address: str) -> bool:
    address = (address or "").strip().lower()
    if not address:
        return True
    local_part = address.split("@", 1)[0]
    return any(
        marker in local_part or marker == address for marker in MACHINE_LOCAL_PARTS
    )


def is_automated_message(headers: dict) -> bool:
    """True if the message advertises itself as bulk or machine-generated."""
    lowered = {key.lower(): (value or "") for key, value in (headers or {}).items()}

    for header in ("list-unsubscribe", "list-id", "x-autoreply", "x-autorespond"):
        if lowered.get(header):
            return True

    # Auto-Submitted: no means a human sent it. Anything else is a machine.
    auto_submitted = lowered.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted not in AUTO_SUBMITTED_HUMAN_VALUES:
        return True

    precedence = lowered.get("precedence", "").strip().lower()
    if precedence in ("bulk", "list", "junk", "auto_reply"):
        return True

    suppress = lowered.get("x-auto-response-suppress", "").strip().lower()
    if suppress and suppress != "none":
        return True

    return False


def structural_check(*, sender: str, headers: dict, own_addresses: tuple) -> Decision:
    """The checks that need no model call, so they can run first.

    Kept separate from should_reply so an obvious newsletter is filed without
    ever reaching the classifier. These are also the only checks that are
    certain: a message from mailer-daemon is not a judgement call.
    """
    address = (sender or "").strip().lower()

    if not address:
        return Decision(False, "no sender address")

    # Never reply to ourselves. This is the loop that empties a mailbox and a
    # sending reputation at the same time.
    if address in tuple(item.lower() for item in own_addresses):
        return Decision(False, "sender is us")

    if is_machine_sender(address):
        return Decision(False, "machine sender (" + address + ")")

    if is_automated_message(headers):
        return Decision(False, "message is bulk or auto-submitted")

    return Decision(True, "from a human address")


def should_reply(
    *,
    sender: str,
    headers: dict,
    classification: str,
    own_addresses: tuple,
    lead_sends_today: int,
    sends_this_run: int,
    max_sends_per_run: int,
    max_sends_per_thread_per_day: int,
) -> Decision:
    """Gate 1 in full: structure, then classification, then rate limits."""
    structural = structural_check(
        sender=sender, headers=headers, own_addresses=own_addresses
    )
    if not structural:
        return structural

    # The send policy: genuine inbound gets a reply, everything else is filed
    # in silence. Unclassifiable mail counts as everything else -- when the
    # agent is unsure, it says nothing rather than guessing out loud.
    if classification != "genuine":
        return Decision(False, "classified as " + classification)

    if sends_this_run >= max_sends_per_run:
        return Decision(
            False,
            "run send limit reached (" + str(max_sends_per_run) + ")",
        )

    if lead_sends_today >= max_sends_per_thread_per_day:
        return Decision(
            False,
            "thread already had "
            + str(lead_sends_today)
            + " replies in 24h (limit "
            + str(max_sends_per_thread_per_day)
            + ")",
        )

    return Decision(True, "genuine inbound from a human")


def day_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Gate 2: is this specific draft safe to send?
# --------------------------------------------------------------------------

# DataRail promises a fixed quote in writing after the intro call. An
# autonomous agent naming a number would be making that promise early, in
# writing, without anyone reading it. So the agent never states an amount.
#
# The model is instructed not to. This catches the case where it does anyway.
MONEY_PATTERNS = (
    # $500, $1,200.00, $2k
    re.compile(r"\$\s?\d[\d,]*(\.\d{2})?\s?[kKmM]?\b"),
    # 500 dollars / 1200 USD / 40 euros
    re.compile(r"\b\d[\d,]*\s?(dollars?|usd|eur|euros?|gbp|pounds?)\b", re.I),
    # "our rate is", "we charge", "costs about"
    re.compile(r"\b(our|the)\s+(hourly\s+)?(rate|fee|price|pricing)\s+(is|starts|would be)\b", re.I),
    re.compile(r"\bwe\s+(charge|bill)\b", re.I),
    re.compile(r"\b(per|an)\s+hour\b.{0,20}\d", re.I),
    re.compile(r"\b\d[\d,]*\s?(per|/)\s?(hour|hr|day|week|month|project)\b", re.I),
)

# Commitments that are not the agent's to make.
COMMITMENT_PATTERNS = (
    re.compile(r"\b(we|I)\s+(guarantee|warrant|promise)\b", re.I),
    re.compile(r"\bguaranteed?\s+(delivery|results?|by)\b", re.I),
    re.compile(r"\b(sign|signed|executed)\s+(the\s+)?(contract|agreement|nda)\b", re.I),
    re.compile(r"\b(refund|money[- ]back)\b", re.I),
    re.compile(r"\b(we|I)\s+(accept|agree to)\s+(your|the)\s+(terms|offer)\b", re.I),
)

# Text that suggests the model is talking about itself rather than to a client.
LEAK_PATTERNS = (
    re.compile(r"\b(as an? (AI|language model|assistant))\b", re.I),
    re.compile(r"\b(system prompt|my instructions|I was told to)\b", re.I),
    re.compile(r"^\s*(here('s| is) (a|the) (draft|reply|response))", re.I),
    re.compile(r"\[(INSERT|YOUR NAME|TODO|PLACEHOLDER)[^\]]*\]", re.I),
)

MAX_REPLY_CHARS = 2400


def vet_reply(body: str, subject: str = "") -> Decision:
    """Gate 2, applied to the drafted text before it reaches SMTP.

    A failure here is not a retry. The draft is stored on the lead, the lead is
    flagged, and a human decides -- because a model that just produced an
    unsafe draft is not the thing to ask for a safer one.
    """
    text = (body or "").strip()

    if not text:
        return Decision(False, "empty draft")

    if len(text) > MAX_REPLY_CHARS:
        return Decision(
            False,
            "draft is " + str(len(text)) + " chars (limit " + str(MAX_REPLY_CHARS) + ")",
        )

    combined = subject + "\n" + text

    for pattern in MONEY_PATTERNS:
        found = pattern.search(combined)
        if found:
            return Decision(False, "names a price: " + repr(found.group(0)))

    for pattern in COMMITMENT_PATTERNS:
        found = pattern.search(combined)
        if found:
            return Decision(False, "makes a commitment: " + repr(found.group(0)))

    for pattern in LEAK_PATTERNS:
        found = pattern.search(combined)
        if found:
            return Decision(False, "leaks scaffolding: " + repr(found.group(0)))

    return Decision(True, "clean")


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_lead(
    *,
    need: str,
    scope: str,
    budget: str,
    timeline: str,
    decision_maker: str,
    company: str,
) -> int:
    """A transparent 0-100 score.

    Deliberately arithmetic rather than a model judgement: the dashboard sorts
    on this, and a number you cannot explain is a number you cannot trust when
    deciding who to call back first.
    """
    score = 0
    if need.strip():
        score += 25
    if scope.strip():
        score += 15
    if budget.strip():
        score += 25
    if timeline.strip():
        score += 20
    if decision_maker.strip():
        score += 10
    if company.strip():
        score += 5
    return min(score, 100)


def status_for(score: int, current: str = "new") -> str:
    """Move a lead along the pipeline, without ever walking it backwards.

    Statuses a human set by hand -- won, lost, unqualified -- are final as far
    as the agent is concerned.
    """
    if current in ("won", "lost", "unqualified"):
        return current
    if score >= 75:
        return "qualified"
    if score > 0:
        return "qualifying"
    return "new"


def redact(text: str, limit: int = 4000) -> str:
    """Trim stored message bodies so the lead file stays a reasonable size."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


def first_match(patterns, text: str) -> Optional[str]:
    for pattern in patterns:
        found = pattern.search(text or "")
        if found:
            return found.group(0)
    return None
