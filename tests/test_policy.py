"""Tests for the guardrails.

These are the tests that matter most in this repo. The agent sends mail with no
human in the loop, so a hole here is a hole that reaches a real client.
"""

from datarail_agents.core import policy


def gate(**overrides):
    """should_reply with sensible defaults, so each test states only its point."""
    kwargs = dict(
        sender="someone@example.com",
        headers={},
        classification="genuine",
        own_addresses=("contact@datarail.org",),
        lead_sends_today=0,
        sends_this_run=0,
        max_sends_per_run=10,
        max_sends_per_thread_per_day=2,
    )
    kwargs.update(overrides)
    return policy.should_reply(**kwargs)


class TestStructuralChecks:
    def test_genuine_human_is_allowed(self):
        assert gate()

    def test_never_replies_to_itself(self):
        assert not gate(sender="contact@datarail.org")

    def test_no_sender_is_refused(self):
        assert not gate(sender="")

    def test_machine_local_parts_are_refused(self):
        for address in (
            "no-reply@stripe.com",
            "noreply@github.com",
            "mailer-daemon@example.net",
            "bounces@sendgrid.net",
            "notifications@slack.com",
        ):
            assert not gate(sender=address), address

    def test_mailing_list_headers_are_refused(self):
        assert not gate(headers={"List-Unsubscribe": "<https://example.com/u>"})
        assert not gate(headers={"List-Id": "news.example.com"})

    def test_auto_submitted_no_is_a_human(self):
        # RFC 3834: "no" is what a human-sent message carries.
        assert gate(headers={"Auto-Submitted": "no"})

    def test_auto_submitted_anything_else_is_a_machine(self):
        assert not gate(headers={"Auto-Submitted": "auto-replied"})
        assert not gate(headers={"Auto-Submitted": "auto-generated"})

    def test_bulk_precedence_is_refused(self):
        assert not gate(headers={"Precedence": "bulk"})

    def test_header_matching_is_case_insensitive(self):
        assert not gate(headers={"LIST-UNSUBSCRIBE": "<https://example.com>"})


class TestClassificationGate:
    def test_only_genuine_earns_a_reply(self):
        for label in ("spam", "newsletter", "automated", "personal"):
            assert not gate(classification=label), label

    def test_unknown_label_is_silent(self):
        # Fail closed: a classifier returning nonsense must not mean a reply.
        assert not gate(classification="probably-fine")


class TestRateLimits:
    def test_run_limit_stops_sending(self):
        assert not gate(sends_this_run=10, max_sends_per_run=10)

    def test_thread_limit_stops_sending(self):
        assert not gate(lead_sends_today=2, max_sends_per_thread_per_day=2)

    def test_under_the_limit_still_sends(self):
        assert gate(lead_sends_today=1, max_sends_per_thread_per_day=2)


class TestReplyVetting:
    def test_a_normal_reply_passes(self):
        body = (
            "Thanks for getting in touch. A booking form and an availability "
            "calendar is very much the kind of work I take on.\n\n"
            "Could you tell me roughly when you would want this live, and "
            "whether you already have a domain?\n\nDataRail"
        )
        assert policy.vet_reply(body)

    def test_empty_draft_fails(self):
        assert not policy.vet_reply("")
        assert not policy.vet_reply("   \n  ")

    def test_dollar_amounts_are_blocked(self):
        for body in (
            "That would be around $2,500 all in.",
            "Sites like this start at $800.",
            "Roughly $3k depending on scope.",
        ):
            assert not policy.vet_reply(body), body

    def test_spelled_out_currency_is_blocked(self):
        assert not policy.vet_reply("Somewhere near 2000 dollars.")
        assert not policy.vet_reply("About 1500 USD for the build.")

    def test_rate_language_is_blocked(self):
        assert not policy.vet_reply("Our rate is competitive for this kind of work.")
        assert not policy.vet_reply("We charge by the day for discovery work.")
        assert not policy.vet_reply("That is 95 per hour.")
        assert not policy.vet_reply("Works out at 95 an hour.")
        assert not policy.vet_reply("Around 1200/week for a retainer.")

    def test_booking_replies_are_not_mistaken_for_pricing(self):
        # Regression: an earlier money pattern matched "an hour" followed by
        # any digit within twenty characters, so every reply confirming a call
        # was blocked on the date. Booking is the agent's whole job here.
        for body in (
            "The call is about half an hour. Tuesday 14 October at 2:00 PM ET suits me.",
            "It runs half an hour, and I have 3 slots free next week.",
            "Half an hour is plenty. Shall we say Monday 6 October, 9:00 AM?",
        ):
            verdict = policy.vet_reply(body)
            assert verdict, body + " -> " + verdict.reason

    def test_pointing_at_the_quote_process_is_fine(self):
        # This is the correct answer to a pricing question and must not trip
        # the money patterns.
        body = (
            "I do not publish rates, because a number without the problem "
            "attached is not much use. After the intro call you get a fixed "
            "quote in writing, and the call itself is free.\n\nDataRail"
        )
        assert policy.vet_reply(body), policy.vet_reply(body).reason

    def test_guarantees_are_blocked(self):
        assert not policy.vet_reply("We guarantee it will be live by Friday.")
        assert not policy.vet_reply("I promise the migration will be seamless.")

    def test_accepting_terms_is_blocked(self):
        assert not policy.vet_reply("We accept your terms and can start Monday.")

    def test_model_scaffolding_is_blocked(self):
        assert not policy.vet_reply("As an AI assistant, I can help with that.")
        assert not policy.vet_reply("Here is a draft reply you could send:")
        assert not policy.vet_reply("Thanks [INSERT NAME], I will take a look.")

    def test_overlong_drafts_are_blocked(self):
        assert not policy.vet_reply("word " * 2000)

    def test_subject_line_is_vetted_too(self):
        assert not policy.vet_reply("A normal body.", subject="Your quote: $4,000")


class TestScoring:
    def test_empty_lead_scores_zero(self):
        assert policy.score_lead(
            need="", scope="", budget="", timeline="", decision_maker="", company=""
        ) == 0

    def test_a_complete_lead_scores_full(self):
        assert policy.score_lead(
            need="booking site",
            scope="four pages plus calendar",
            budget="under 5k",
            timeline="within a month",
            decision_maker="yes, owner",
            company="Heather's Face Paint",
        ) == 100

    def test_need_and_budget_carry_the_most(self):
        need_only = policy.score_lead(
            need="a site", scope="", budget="", timeline="", decision_maker="", company=""
        )
        company_only = policy.score_lead(
            need="", scope="", budget="", timeline="", decision_maker="", company="Acme"
        )
        assert need_only > company_only

    def test_score_never_exceeds_one_hundred(self):
        assert policy.score_lead(
            need="x", scope="x", budget="x", timeline="x",
            decision_maker="x", company="x",
        ) <= 100


class TestStatus:
    def test_high_score_qualifies(self):
        assert policy.status_for(80) == "qualified"

    def test_partial_information_is_qualifying(self):
        assert policy.status_for(40) == "qualifying"

    def test_nothing_known_stays_new(self):
        assert policy.status_for(0) == "new"

    def test_human_set_statuses_are_final(self):
        # The agent must never reopen something a person closed.
        for final in ("won", "lost", "unqualified"):
            assert policy.status_for(100, final) == final


class TestRedaction:
    def test_short_text_is_untouched(self):
        assert policy.redact("hello") == "hello"

    def test_long_text_is_truncated_and_marked(self):
        result = policy.redact("x" * 5000, limit=100)
        assert result.endswith("[truncated]")
        assert len(result) < 200
