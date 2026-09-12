"""The email receptionist's main loop.

One pass over unread mail. Runs on a GitHub Actions cron every 30 minutes,
which means this process starts cold, does its work, and exits -- there is no
in-memory state between runs. Everything durable goes in the lead file or in
the IMAP folder a message was moved to.

    python -m datarail_agents.email_agent.run --site ../datarail-site

Every message ends in exactly one of five places, and the run log says which:

  replied   -- draft passed both gates, sent, moved to Agent/Handled
  listing   -- event dates for Glyph, proposed as a branch, moved to Agent/Listings
  review    -- needs a person: a draft that failed the safety gate, or listing
               mail with nothing listable in it; moved to Agent/Review
  ignored   -- spam, bulk or automated, moved to Agent/Ignored, no reply
  error     -- something broke; left in INBOX unread to be retried next run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from ..core import booking as booking_flow
from ..core import knowledge, policy
from ..core import listings as listing_rules
from ..core.brain import Brain, BrainError
from ..core.calendar import Calendar, CalendarError, ics_for
from ..core.config import Config, ConfigError
from ..core.leads import Contact, Interaction, LeadStore
from .glyph import GlyphError, GlyphRepo
from .mailbox import InboundMessage, Mailbox

# The raw store lives OUTSIDE public/, at the datarail-site repo root.
#
# datarail-site only uploads public/ to the webspace, so a lead file kept here
# is in git and in the workflow but never reachable over HTTP -- no matter what
# happens to the .htaccess protecting the dashboard. The dashboard inlines its
# own copy of the data, so nothing is lost by keeping the source private.
LEADS_RELATIVE_PATH = os.path.join("leads", "data.json")
RUNLOG_RELATIVE_PATH = os.path.join("leads", "runlog.json")

# How deep to look when --limit asks for the N most recent messages that need a
# decision. Newsletters and machine mail are skipped for free and do not count
# towards N, so the scan has to be allowed to run past them -- but not forever.
SCAN_DEPTH = 200


def log(message: str) -> None:
    """Print with a flush.

    Actions interleaves stdout and stderr unpredictably when buffered, and a
    run log you cannot trust the order of is not much use at 2am.
    """
    print(message, flush=True)
    sys.stdout.flush()


class Runner:
    def __init__(self, config: Config, site_root: str, glyph_root: str = "",
                 limit: int = 0):
        self.config = config
        self.site_root = site_root
        # 0 means the whole inbox. Anything else is "the N most recent messages
        # that need a decision", counted after the free structural filter.
        self.limit = max(0, int(limit or 0))
        self.considered = 0
        self.leads_path = os.path.join(site_root, LEADS_RELATIVE_PATH)
        self.store = LeadStore.open(self.leads_path)
        self.brain = Brain(config.brain)
        self.knowledge = knowledge.load()
        self.sends_this_run = 0
        self.listings_this_run = 0
        self.results = {
            "replied": [], "listing": [], "review": [], "ignored": [], "error": [], "booked": [],
        }

        # Listing intake for Glyph is optional, like booking. Without a Glyph
        # checkout the rest of the receptionist runs exactly as it always has.
        self.glyph = None
        if glyph_root:
            try:
                self.glyph = GlyphRepo.open(glyph_root)
            except GlyphError as error:
                log("  glyph unavailable, listing intake disabled: " + str(error))

        # Booking is optional. If the calendar cannot be constructed the agent
        # still answers and still qualifies -- it just never offers a time.
        self.calendar = None
        if config.calendar:
            try:
                self.calendar = Calendar(config.calendar)
            except CalendarError as error:
                log("  calendar unavailable, booking disabled: " + str(error))

    # ------------------------------------------------------------------

    def run(self) -> dict:
        log("DataRail email receptionist")
        log("  mailbox : " + self.config.mailbox.address)
        log("  model   : " + self.config.brain.model)
        log("  leads   : " + self.leads_path + " (" + str(len(self.store)) + " existing)")
        log("  mode    : " + ("DRY RUN -- nothing will be sent" if self.config.dry_run else "LIVE"))
        log("  glyph   : " + ("proposing listings from " + self.glyph.root if self.glyph
                              else "off -- listing mail is left unread"))
        log("")

        with Mailbox(self.config.mailbox) as mailbox:
            waiting = mailbox.count_unread()
            if self.limit:
                log("Found " + str(waiting) + " unread. Taking the " + str(self.limit)
                    + " most recent that need a decision -- machine and bulk mail "
                    + "is skipped for free and does not count.")
            else:
                log("Found " + str(waiting) + " unread message(s).")
            log("")

            # Each message is fetched as the loop reaches it, so stopping early
            # costs nothing for the mail left behind.
            for message in mailbox.unread(
                limit=SCAN_DEPTH if self.limit else 50,
                newest_first=bool(self.limit),
            ):
                if self.limit and self.considered >= self.limit:
                    log("Limit reached. The rest stay unread for the next run.")
                    break
                try:
                    if self._handle(mailbox, message):
                        self.considered += 1
                except Exception as error:  # noqa: BLE001 - one bad message must
                    # not take down the run; the rest of the mailbox still needs
                    # answering, and this one is left unread to retry.
                    log("  ERROR " + message.sender_email + ": " + str(error))
                    traceback.print_exc()
                    self.results["error"].append(
                        {"from": message.sender_email, "error": str(error)}
                    )
                    # Counted: a message that breaks every time must not push the
                    # scan deeper on each run while it looks for its quota.
                    self.considered += 1

        self.store.save()
        self._write_runlog()
        self._summarise()
        return self.results

    # ------------------------------------------------------------------

    def _handle(self, mailbox: Mailbox, message: InboundMessage) -> bool:
        """Deal with one message. Returns whether it needed a decision.

        False means the structural filter recognised it as machine or bulk mail
        and it cost nothing to dismiss, so --limit does not count it. Everything
        else counts, including mail the classifier then files in silence: that
        one already spent a model call.
        """
        who = message.sender_email or "(no sender)"
        log("- " + who + " | " + (message.subject or "(no subject)")[:60])

        # Cheap structural checks run before the model is asked anything, so an
        # obvious newsletter costs nothing.
        structural = policy.structural_check(
            sender=message.sender_email,
            headers=message.headers,
            own_addresses=(self.config.mailbox.address, self.config.operator_email),
        )
        if not structural:
            log("    ignored: " + structural.reason)
            self._file(mailbox, message, self.config.mailbox.ignored_folder)
            self.results["ignored"].append({"from": who, "reason": structural.reason})
            return False

        classification = self.brain.classify(
            sender=message.sender_email,
            subject=message.subject,
            body=message.body,
        )
        log("    classified: " + classification)

        # Listings for Glyph never reach the reply path. They are not leads, and
        # a venue sending its dates must not get a consulting pitch back.
        if classification == "listing":
            self._handle_listing(mailbox, message)
            return True

        contact = Contact(
            name=message.sender_name,
            email=message.sender_email,
        )
        lead = self.store.find_by_thread(message.thread_id) or self.store.find_by_contact(contact)

        decision = policy.should_reply(
            sender=message.sender_email,
            headers=message.headers,
            classification=classification,
            own_addresses=(self.config.mailbox.address, self.config.operator_email),
            lead_sends_today=lead.sends_since(policy.day_ago_iso()) if lead else 0,
            sends_this_run=self.sends_this_run,
            max_sends_per_run=self.config.max_sends_per_run,
            max_sends_per_thread_per_day=self.config.max_sends_per_thread_per_day,
        )

        if not decision:
            log("    no reply: " + decision.reason)
            self._file(mailbox, message, self.config.mailbox.ignored_folder)
            self.results["ignored"].append({"from": who, "reason": decision.reason})
            return True

        # From here on this is a real enquiry and gets a lead record whatever
        # happens to the draft.
        lead = self.store.upsert("email", contact, thread_id=message.thread_id)
        lead.add_interaction(Interaction(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source="email",
            direction="in",
            subject=message.subject,
            body=policy.redact(message.body),
            message_id=message.message_id,
        ))

        offer = self._offer_for(lead)
        draft = self._draft(lead, message, offer)

        self._apply_extraction(lead, draft)

        # If they picked a time, book it before the reply goes out -- the reply
        # says the call is confirmed, so it must be true by the time it sends.
        ics = ""
        if draft.chosen_slot and self.calendar and self.config.calendar:
            slot, draft, offer = self._book(lead, message, draft, offer)
            if slot is not None:
                ics = ics_for(
                    slot=slot,
                    summary="DataRail intro call",
                    description=booking_flow.invite_text(lead, slot, self.config.calendar),
                    organiser_email=self.config.mailbox.address,
                    client_email=lead.contact.email or message.sender_email,
                    client_name=lead.contact.name or message.sender_name,
                    tz_name=self.config.calendar.timezone,
                )

        body = draft.body
        if self.config.disclose_agent:
            body = body.rstrip() + "\n\n--\n" + self.config.disclosure_line

        verdict = policy.vet_reply(body, draft.subject)
        if not verdict:
            log("    HELD for review: " + verdict.reason)
            # Status stays whatever the extraction just worked out. A held
            # draft says something about the draft, not about the lead, and
            # must not walk a qualified lead backwards.
            lead.open_questions = list(lead.open_questions) + [
                "Agent draft held for review: " + verdict.reason
            ]
            self._file(mailbox, message, self.config.mailbox.review_folder)
            self.results["review"].append({"from": who, "reason": verdict.reason})
            return True

        subject = draft.subject or _reply_subject(message.subject)

        if self.config.dry_run:
            log("    would send: " + subject)
            log("    " + body.replace("\n", "\n    ")[:600])
            if self.config.alert_address:
                log("    would alert " + self.config.alert_address)
            self.results["replied"].append({"from": who, "subject": subject, "dry_run": True})
            # Nothing is filed or marked in a dry run: the message stays unread
            # so the first live run still answers it.
            return True

        sent_id = mailbox.send_reply(
            to_address=message.sender_email,
            to_name=message.sender_name,
            subject=subject,
            body=body,
            in_reply_to=message.message_id,
            references=message.references,
            ics=ics,
        )
        self.sends_this_run += 1
        log("    replied: " + subject + (" (+invite)" if ics else ""))

        lead.add_interaction(Interaction(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source="email",
            direction="out",
            subject=subject,
            body=policy.redact(body),
            message_id=sent_id,
        ))
        self._file(mailbox, message, self.config.mailbox.processed_folder)
        self.results["replied"].append({"from": who, "subject": subject, "score": lead.score})
        self._alert(mailbox, message, subject, body, booked=bool(ics), score=lead.score)
        return True

    def _alert(self, mailbox: Mailbox, message: InboundMessage, subject: str,
               body: str, *, booked: bool, score: int) -> None:
        """Tell the operator a reply went out. Never breaks the run.

        The reply has already been sent by the time this runs, so a failure here
        must not raise: the client has their answer either way, and letting the
        run die would leave the message unfiled and answer it twice on the next
        pass. A failed alert is a warning in the log, nothing more.
        """
        address = self.config.alert_address.strip()
        if not address:
            return
        if address.lower() == self.config.mailbox.address.strip().lower():
            # Alerting the mailbox it reads would put the alert back in the
            # inbox, to be classified and possibly answered. That is the loop.
            log("    not alerting: the alert address is the mailbox itself")
            return
        try:
            mailbox.send_alert(
                to_address=address,
                subject="[DataRail] Replied to " + (message.sender_email or "someone"),
                body=alert_text(
                    message=message, subject=subject, body=body,
                    booked=booked, score=score,
                ),
            )
            log("    alerted " + address)
        except Exception as error:  # noqa: BLE001 - see the docstring
            log("    warning: could not send the alert: " + str(error))

    # ------------------------------------------------------------------

    def _handle_listing(self, mailbox: Mailbox, message: InboundMessage) -> None:
        """Draft an email's events and propose them to Glyph as a branch.

        Never replies. The sender finds out the way everyone else does: the
        listing appears on the calendar once a person has approved it.
        """
        who = message.sender_email or "(no sender)"

        if self.glyph is None:
            # Neither filed nor marked read: nothing is lost to a missing key,
            # and the first run after GLYPH_DEPLOY_KEY is set picks these up.
            log("    listing intake is off (no Glyph checkout); left unread")
            self.results["listing"].append({"from": who, "outcome": "intake off"})
            return

        if self.listings_this_run >= self.config.max_listings_per_run:
            log("    listing limit for this run reached; left for the next run")
            self.results["listing"].append({"from": who, "outcome": "run limit"})
            return

        ref = listing_rules.proposal_ref(
            message.message_id or (who + "|" + message.subject + "|" + message.date)
        )
        branch = listing_rules.branch_for(ref)
        if self.glyph.already_proposed(branch):
            log("    already proposed as " + branch)
            self._file(mailbox, message, self.config.mailbox.listings_folder)
            self.results["listing"].append(
                {"from": who, "outcome": "already proposed", "ref": ref}
            )
            return

        draft = self.brain.extract_listings(
            subject=message.subject,
            body=message.body,
            venues=list(self.glyph.venues.values()),
            today=listing_rules.today_in(self._timezone()),
        )
        shaped = [listing_rules.shape(item, self.glyph.venues) for item in draft.listings]
        shaped = [item for item in shaped if item.get("title")]

        if not shaped:
            # A question about listings, a removal request, or an email the model
            # could not read. Each of those needs a person, not a pull request.
            reason = "listing mail with nothing to list"
            if draft.notes:
                reason += ": " + draft.notes[:200]
            log("    " + reason)
            self._file(mailbox, message, self.config.mailbox.review_folder)
            self.results["review"].append({"from": who, "reason": reason})
            return

        named = [self.glyph.venues[item["venueId"]] for item in shaped if item.get("venueId")]
        subject, body = listing_rules.commit_message(
            shaped,
            received=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            ref=ref,
            sender_matches=listing_rules.sender_is_venue(message.sender_email, named),
        )
        # The run log is private -- it lives outside public/ in datarail-site --
        # so the sender and the model's free-text notes belong here, and only
        # here. The pull request gets the listing and nothing else.
        record = {"from": who, "ref": ref, "count": len(shaped),
                  "subject": subject, "notes": draft.notes}

        if self.config.dry_run:
            log("    would propose " + branch + ": " + subject)
            for item in shaped:
                log("      " + json.dumps(item, ensure_ascii=False)[:400])
            self.results["listing"].append(dict(record, outcome="dry run"))
            return

        self.glyph.propose(branch, shaped, subject, body)
        self.listings_this_run += 1
        log("    proposed " + branch + " (" + str(len(shaped)) + " listing(s))")
        self._file(mailbox, message, self.config.mailbox.listings_folder)
        self.results["listing"].append(dict(record, outcome="proposed", branch=branch))

    def _timezone(self) -> str:
        if self.config.calendar:
            return self.config.calendar.timezone
        # `or`, not a default: Actions passes an unset variable as "".
        return os.environ.get("DATARAIL_TIMEZONE") or "America/New_York"

    def _offer_for(self, lead):
        """The times to put in front of this lead, if any.

        Reuses a standing offer so the numbering the client is replying to
        still means what it meant. Only generates a fresh set when there is
        nothing live to reuse.
        """
        if not self.calendar or not self.config.calendar:
            return booking_flow.Offer(slots=[], descriptions=[])

        if lead.booking.status == "booked":
            return booking_flow.Offer(slots=[], descriptions=[])

        standing = booking_flow.current_offer(lead, self.config.calendar)
        if not standing.is_empty():
            return standing

        fresh = booking_flow.make_offer(self.calendar, lead, self.config.calendar)
        if fresh.is_empty():
            log("    no free slots to offer")
        return fresh

    def _draft(self, lead, message: InboundMessage, offer, conflict: bool = False):
        booked_when = ""
        if lead.booking.status == "booked" and self.config.calendar:
            from ..core.calendar import Slot, describe

            try:
                booked_when = describe(
                    Slot.from_iso(lead.booking.slot), self.config.calendar.timezone
                )
            except (KeyError, ValueError):
                booked_when = "a time already in the diary"

        return self.brain.draft(
            sender=message.sender_email,
            subject=message.subject,
            body=message.body,
            history=self._history_for(lead),
            known=self._known_for(lead),
            knowledge=self.knowledge,
            operator_name=self.config.operator_name,
            signature_name=self.config.signature_name,
            offer_text=offer.numbered(),
            booking_state=lead.booking.status,
            booked_when=booked_when,
            conflict=conflict,
        )

    def _book(self, lead, message: InboundMessage, draft, offer):
        """Book the chosen slot, or recover if it has gone.

        Returns (slot, draft, offer). A None slot means nothing was booked and
        the draft has been rewritten so it does not claim otherwise.
        """
        if self.config.dry_run:
            log("    would book slot " + str(draft.chosen_slot))
            return None, draft, offer

        try:
            slot = booking_flow.confirm(
                self.calendar, lead, self.config.calendar, draft.chosen_slot
            )
        except booking_flow.BookingError as error:
            log("    could not book: " + str(error))

            # The draft in hand confirms a call that is not happening, so it
            # cannot be sent. Offer fresh times and write the reply again --
            # one extra model call on a path that should be rare.
            lead.booking.status = "none"
            replacement = booking_flow.make_offer(
                self.calendar, lead, self.config.calendar
            )
            redraft = self._draft(lead, message, replacement, conflict=True)
            self._apply_extraction(lead, redraft)
            self.results["booked"].append({
                "from": lead.contact.email, "outcome": "conflict", "reason": str(error),
            })
            return None, redraft, replacement

        log("    BOOKED: " + slot.start.isoformat())
        self.results["booked"].append({
            "from": lead.contact.email,
            "outcome": "confirmed",
            "at": slot.start.isoformat(),
        })
        return slot, draft, offer

    def _file(self, mailbox: Mailbox, message: InboundMessage, folder: str) -> None:
        """Mark handled and move out of INBOX.

        Seen is set first and unconditionally. If the move fails -- a folder
        that could not be created, a server hiccup -- the message still will
        not be picked up by the next run, which is the outcome that matters.
        """
        if self.config.dry_run:
            return
        try:
            mailbox.mark_seen(message.uid)
        except Exception as error:  # noqa: BLE001
            log("    warning: could not mark seen: " + str(error))
        if not mailbox.move(message.uid, folder):
            log("    warning: could not move to " + folder + " (left in INBOX, marked read)")

    def _history_for(self, lead) -> str:
        """The last few turns of this conversation, oldest first."""
        lines = []
        for item in list(lead.interactions)[-6:]:
            direction = item.get("direction") if isinstance(item, dict) else item.direction
            subject = item.get("subject") if isinstance(item, dict) else item.subject
            body = item.get("body") if isinstance(item, dict) else item.body
            who = "THEM" if direction == "in" else "DATARAIL"
            lines.append(who + " -- " + (subject or "") + "\n" + (body or "")[:1200])
        return "\n\n".join(lines)

    @staticmethod
    def _known_for(lead) -> dict:
        return {
            "name": lead.contact.name,
            "email": lead.contact.email,
            "company": lead.contact.company,
            "need": lead.need,
            "scope": lead.scope,
            "budget": lead.budget,
            "timeline": lead.timeline,
            "decision_maker": lead.decision_maker,
            "current_score": lead.score,
        }

    @staticmethod
    def _apply_extraction(lead, draft) -> None:
        """Fold what the model learned into the lead.

        Only ever fills blanks. A later message that omits the budget must not
        erase a budget the client already gave, and the model does re-emit
        empty fields.
        """
        if draft.name and not lead.contact.name:
            lead.contact.name = draft.name
        if draft.company and not lead.contact.company:
            lead.contact.company = draft.company

        for attribute in ("need", "scope", "budget", "timeline", "decision_maker"):
            incoming = getattr(draft, attribute, "").strip()
            if incoming and not getattr(lead, attribute, "").strip():
                setattr(lead, attribute, incoming)

        # The summary is the one field a newer read should win, since it
        # describes the conversation as a whole.
        if draft.summary:
            lead.summary = draft.summary
        if draft.open_questions:
            lead.open_questions = draft.open_questions

        lead.score = policy.score_lead(
            need=lead.need,
            scope=lead.scope,
            budget=lead.budget,
            timeline=lead.timeline,
            decision_maker=lead.decision_maker,
            company=lead.contact.company,
        )
        lead.status = policy.status_for(lead.score, lead.status)
        lead.touch()

    def _write_runlog(self) -> None:
        path = os.path.join(self.site_root, RUNLOG_RELATIVE_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dry_run": self.config.dry_run,
            "model": self.config.brain.model,
            "counts": {key: len(value) for key, value in self.results.items()},
            "results": self.results,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    def _summarise(self) -> None:
        log("")
        confirmed = sum(
            1 for item in self.results["booked"] if item.get("outcome") == "confirmed"
        )
        proposed = sum(
            1 for item in self.results["listing"]
            if item.get("outcome") in ("proposed", "dry run")
        )
        log("Replied " + str(len(self.results["replied"]))
            + " | listings proposed " + str(proposed)
            + " | booked " + str(confirmed)
            + " | held for review " + str(len(self.results["review"]))
            + " | ignored " + str(len(self.results["ignored"]))
            + " | errors " + str(len(self.results["error"])))
        log("Leads on file: " + str(len(self.store)))


def alert_text(*, message, subject: str, body: str, booked: bool, score: int) -> str:
    """What the operator's copy of an outgoing reply says.

    A copy for the record rather than a task: it opens by saying nothing is
    needed, because an alert that reads like a to-do turns into one.
    """
    sender = message.sender_email or "(no sender)"
    if message.sender_name:
        sender = message.sender_name + " <" + sender + ">"

    lines = [
        "The receptionist has answered a message. Nothing is needed from you --",
        "this is your copy of what went out.",
        "",
        "From:    " + sender,
        "About:   " + (message.subject or "(no subject)"),
        "Sent as: " + subject,
        "Lead:    scored " + str(score) + " out of 100",
    ]
    if booked:
        lines.append("Booked:  intro call confirmed, invitation attached to the reply")
    lines += ["", "---- what was sent ----", "", body.strip(), ""]
    return "\n".join(lines)


def _reply_subject(original: str) -> str:
    original = (original or "").strip() or "Your enquiry"
    if original.lower().startswith("re:"):
        return original
    return "Re: " + original


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the DataRail email receptionist once.")
    parser.add_argument(
        "--site",
        default=os.environ.get("DATARAIL_SITE_ROOT", "site"),
        help="Path to a datarail-site checkout. Leads are written under its public/leads/.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually send. Without this the run is a dry run whatever the environment says.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check credentials, models and knowledge base, then exit without touching mail.",
    )
    parser.add_argument(
        "--glyph",
        default=os.environ.get("GLYPH_ROOT", ""),
        help="Path to a fvtale/glyph checkout. Without one, listing intake is off.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only handle the N most recent messages that need a decision, "
             "newest first. Machine and bulk mail is skipped without counting. "
             "0, the default, means the whole inbox oldest first.",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.load()
    except ConfigError as error:
        log("Configuration error: " + str(error))
        return 2

    # --live is the only way to send. The environment variable can silence the
    # agent but cannot switch it on, so a mis-set repository variable fails
    # quiet rather than loud.
    if not args.live:
        config = _as_dry_run(config)

    if args.doctor:
        return _doctor(config, args.site, args.glyph)

    try:
        Runner(config, args.site, glyph_root=args.glyph, limit=args.limit).run()
    except BrainError as error:
        log("Model error: " + str(error))
        return 3
    except Exception as error:  # noqa: BLE001
        log("Run failed: " + str(error))
        traceback.print_exc()
        return 1
    return 0


def _as_dry_run(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, dry_run=True)


def _doctor(config: Config, site_root: str, glyph_root: str = "") -> int:
    ok = True

    log("Knowledge base")
    try:
        text = knowledge.load()
        log("  ok: " + str(len(text)) + " characters")
    except Exception as error:  # noqa: BLE001
        ok = False
        log("  FAIL: " + str(error))

    log("Lead store")
    path = os.path.join(site_root, LEADS_RELATIVE_PATH)
    if os.path.exists(path):
        store = LeadStore.open(path)
        log("  ok: " + str(len(store)) + " leads at " + path)
    else:
        log("  ok: no file yet, will be created at " + path)

    log("OpenAI")
    try:
        report = Brain(config.brain).doctor()
        if report["ok"]:
            log("  ok: " + config.brain.model + " and " + config.brain.classifier_model + " available")
        else:
            ok = False
            for problem in report["problems"]:
                log("  FAIL: " + problem)
            log("  Set OPENAI_MODEL / OPENAI_CLASSIFIER_MODEL to one of the "
                "models this key can reach.")
    except Exception as error:  # noqa: BLE001
        ok = False
        log("  FAIL: " + str(error))

    log("Calendar")
    if not config.calendar:
        log("  off: GOOGLE_SERVICE_ACCOUNT_JSON is not set, so the agent will "
            "answer but never offer a time")
    else:
        try:
            report = Calendar(config.calendar).doctor()
            if report["ok"]:
                log("  ok: " + config.calendar.calendar_id + " -- " + report["detail"])
            else:
                ok = False
            for problem in report["problems"]:
                log("  " + ("FAIL: " if not report["ok"] else "warning: ") + problem)
        except CalendarError as error:
            ok = False
            log("  FAIL: " + str(error))

    log("Alerts")
    if not config.alert_address:
        log("  off: DATARAIL_ALERT_ADDRESS is not set, so nothing is sent when a "
            "reply goes out")
    elif config.alert_address.strip().lower() == config.mailbox.address.strip().lower():
        ok = False
        log("  FAIL: the alert address is the mailbox itself, which would put "
            "every alert back in the inbox")
    else:
        # Not printed. It is a secret precisely so it stays out of a public log,
        # and Actions would mask it here anyway.
        log("  ok: a copy of every reply goes to the alert address")

    log("Glyph")
    if not glyph_root:
        log("  off: no Glyph checkout, so listing mail is left unread until "
            "GLYPH_DEPLOY_KEY is set")
    else:
        try:
            repo = GlyphRepo.open(glyph_root)
            if repo is None:
                ok = False
                log("  FAIL: " + glyph_root + " is not a git checkout")
            else:
                # ls-remote goes over SSH with the deploy key, so this proves the
                # key authenticates -- without pushing anything to find out.
                repo.already_proposed("listings/doctor-check")
                log("  ok: deploy key reaches Glyph; " + str(len(repo.venues))
                    + " venues in the registry")
        except GlyphError as error:
            ok = False
            log("  FAIL: " + str(error))

    log("Mailbox")
    try:
        with Mailbox(config.mailbox) as mailbox:
            count = mailbox.count_unread()
            log("  ok: connected to " + config.mailbox.imap_host
                + ", " + str(count) + " unread")
    except Exception as error:  # noqa: BLE001
        ok = False
        log("  FAIL: " + str(error))

    log("")
    log("Doctor: " + ("all checks passed" if ok else "problems found"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
