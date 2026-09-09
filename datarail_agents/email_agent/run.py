"""The email receptionist's main loop.

One pass over unread mail. Runs on a GitHub Actions cron every 30 minutes,
which means this process starts cold, does its work, and exits -- there is no
in-memory state between runs. Everything durable goes in the lead file or in
the IMAP folder a message was moved to.

    python -m datarail_agents.email_agent.run --site ../datarail-site

Every message ends in exactly one of four places, and the run log says which:

  replied   -- draft passed both gates, sent, moved to Agent/Handled
  review    -- real enquiry, draft failed the safety gate, moved to Agent/Review
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

from ..core import knowledge, policy
from ..core.brain import Brain, BrainError
from ..core.config import Config, ConfigError
from ..core.leads import Contact, Interaction, LeadStore
from .mailbox import InboundMessage, Mailbox

# The raw store lives OUTSIDE public/, at the datarail-site repo root.
#
# datarail-site only uploads public/ to the webspace, so a lead file kept here
# is in git and in the workflow but never reachable over HTTP -- no matter what
# happens to the .htaccess protecting the dashboard. The dashboard inlines its
# own copy of the data, so nothing is lost by keeping the source private.
LEADS_RELATIVE_PATH = os.path.join("leads", "data.json")
RUNLOG_RELATIVE_PATH = os.path.join("leads", "runlog.json")


def log(message: str) -> None:
    """Print with a flush.

    Actions interleaves stdout and stderr unpredictably when buffered, and a
    run log you cannot trust the order of is not much use at 2am.
    """
    print(message, flush=True)
    sys.stdout.flush()


class Runner:
    def __init__(self, config: Config, site_root: str):
        self.config = config
        self.site_root = site_root
        self.leads_path = os.path.join(site_root, LEADS_RELATIVE_PATH)
        self.store = LeadStore.open(self.leads_path)
        self.brain = Brain(config.brain)
        self.knowledge = knowledge.load()
        self.sends_this_run = 0
        self.results = {"replied": [], "review": [], "ignored": [], "error": []}

    # ------------------------------------------------------------------

    def run(self) -> dict:
        log("DataRail email receptionist")
        log("  mailbox : " + self.config.mailbox.address)
        log("  model   : " + self.config.brain.model)
        log("  leads   : " + self.leads_path + " (" + str(len(self.store)) + " existing)")
        log("  mode    : " + ("DRY RUN -- nothing will be sent" if self.config.dry_run else "LIVE"))
        log("")

        with Mailbox(self.config.mailbox) as mailbox:
            messages = list(mailbox.unread())
            log("Found " + str(len(messages)) + " unread message(s).")
            log("")

            for message in messages:
                try:
                    self._handle(mailbox, message)
                except Exception as error:  # noqa: BLE001 - one bad message must
                    # not take down the run; the rest of the mailbox still needs
                    # answering, and this one is left unread to retry.
                    log("  ERROR " + message.sender_email + ": " + str(error))
                    traceback.print_exc()
                    self.results["error"].append(
                        {"from": message.sender_email, "error": str(error)}
                    )

        self.store.save()
        self._write_runlog()
        self._summarise()
        return self.results

    # ------------------------------------------------------------------

    def _handle(self, mailbox: Mailbox, message: InboundMessage) -> None:
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
            return

        classification = self.brain.classify(
            sender=message.sender_email,
            subject=message.subject,
            body=message.body,
        )
        log("    classified: " + classification)

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
            return

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

        draft = self.brain.draft(
            sender=message.sender_email,
            subject=message.subject,
            body=message.body,
            history=self._history_for(lead),
            known=self._known_for(lead),
            knowledge=self.knowledge,
            operator_name=self.config.operator_name,
            signature_name=self.config.signature_name,
        )

        self._apply_extraction(lead, draft)

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
            return

        subject = draft.subject or _reply_subject(message.subject)

        if self.config.dry_run:
            log("    would send: " + subject)
            log("    " + body.replace("\n", "\n    ")[:600])
            self.results["replied"].append({"from": who, "subject": subject, "dry_run": True})
            # Nothing is filed or marked in a dry run: the message stays unread
            # so the first live run still answers it.
            return

        sent_id = mailbox.send_reply(
            to_address=message.sender_email,
            to_name=message.sender_name,
            subject=subject,
            body=body,
            in_reply_to=message.message_id,
            references=message.references,
        )
        self.sends_this_run += 1
        log("    replied: " + subject)

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

    # ------------------------------------------------------------------

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
        log("Replied " + str(len(self.results["replied"]))
            + " | held for review " + str(len(self.results["review"]))
            + " | ignored " + str(len(self.results["ignored"]))
            + " | errors " + str(len(self.results["error"])))
        log("Leads on file: " + str(len(self.store)))


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
        return _doctor(config, args.site)

    try:
        Runner(config, args.site).run()
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


def _doctor(config: Config, site_root: str) -> int:
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
