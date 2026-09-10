"""Tests for Glyph listing intake.

Listings go to a public repository, so most of what is tested here is about
what must never reach it: a sender's address, their phone number, the model's
free-text notes, a field outside the listing contract. The rest is the git
path, run against a real repository on disk, because it is the part that
cannot be read for correctness.
"""

import json
import subprocess

import pytest

from datarail_agents.core import listings, policy
from datarail_agents.core.brain import (
    CLASSIFICATIONS,
    MAX_LISTINGS_PER_EMAIL,
    listing_draft_from,
)
from datarail_agents.email_agent.glyph import AUTHOR, GlyphRepo

VENUE = {
    "id": "kgb-bar", "name": "KGB Bar", "neighborhood": "East Village",
    "region": "manhattan", "site": "https://www.kgbbar.com",
}
VENUES = {"kgb-bar": VENUE}


def raw(**overrides):
    base = {
        "title": "Prose night, three readers", "kind": "reading",
        "date": "2026-09-20", "time": "19:00", "venueId": "kgb-bar",
        "url": "https://www.kgbbar.com/events/prose", "price": "Free",
        "description": "Short fiction, twelve minutes each.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# the classifier's new label
# ---------------------------------------------------------------------------

class TestLabel:
    def test_listing_is_a_label_the_classifier_may_return(self):
        assert "listing" in CLASSIFICATIONS

    def test_a_listing_never_earns_a_reply(self):
        # Routed to the Glyph intake before this gate, but if it ever reached
        # it, a venue sending its dates must get silence, not a sales pitch.
        decision = policy.should_reply(
            sender="events@kgbbar.com", headers={}, classification="listing",
            own_addresses=("contact@datarail.org",), lead_sends_today=0,
            sends_this_run=0, max_sends_per_run=10, max_sends_per_thread_per_day=2,
        )
        assert not decision


# ---------------------------------------------------------------------------
# coercing what the model returned
# ---------------------------------------------------------------------------

class TestDraftCoercion:
    def test_a_well_formed_answer_passes_through(self):
        draft = listing_draft_from({"listings": [raw()], "notes": "all clear"})
        assert len(draft.listings) == 1 and draft.notes == "all clear"

    def test_anything_not_a_list_of_objects_contributes_nothing(self):
        assert listing_draft_from({"listings": "a reading on Tuesday"}).listings == []
        assert listing_draft_from({"listings": ["text", 3, None]}).listings == []
        assert listing_draft_from(["not", "a", "dict"]).listings == []

    def test_a_season_is_capped(self):
        draft = listing_draft_from({"listings": [raw()] * 50})
        assert len(draft.listings) == MAX_LISTINGS_PER_EMAIL


# ---------------------------------------------------------------------------
# shaping one listing
# ---------------------------------------------------------------------------

class TestShape:
    def test_a_good_listing_gets_its_venue_from_the_registry(self):
        item = listings.shape(raw(), VENUES)
        assert item["venue"] == "KGB Bar"
        assert item["region"] == "manhattan"
        assert item["neighborhood"] == "East Village"

    def test_ids_match_glyph_exactly(self):
        # Same fixture as Glyph's own test of make_id, so the two can't drift.
        assert listings.shape(raw(), VENUES)["id"] == "kgb-bar-2026-09-20-prose-night-three-readers"

    def test_fields_outside_the_contract_are_dropped(self):
        item = listings.shape(raw(senderEmail="me@home.net", organiser="Jane Doe"), VENUES)
        assert "senderEmail" not in item and "organiser" not in item

    def test_contact_details_are_scrubbed_from_public_text(self):
        item = listings.shape(raw(
            description="Doors at 7. Questions to jane@home.net or (718) 555-0142.",
            price="$10, call 718.555.0142",
        ), VENUES)
        public = json.dumps(item)
        assert "jane@home.net" not in public
        assert "555" not in public
        assert "Doors at 7." in item["description"]

    def test_a_venue_not_on_glyph_is_left_for_a_person(self):
        item = listings.shape(raw(venueId="made-up", venueName="Cornelia Street Café"), VENUES)
        assert item["venueId"] == ""
        assert item["venue"] == "Cornelia Street Café"
        assert "region" not in item

    def test_an_unknown_kind_is_not_quietly_repaired(self):
        # Glyph's intake flags it for a person; this module does not guess.
        assert listings.shape(raw(kind="Concert"), VENUES)["kind"] == "concert"

    def test_a_link_that_is_not_a_web_link_is_dropped(self):
        assert "url" not in listings.shape(raw(url="mailto:jane@home.net"), VENUES)
        assert "url" not in listings.shape(raw(url="javascript:alert(1)"), VENUES)

    def test_long_text_is_capped(self):
        item = listings.shape(raw(title="x" * 1000, description="y" * 1000), VENUES)
        assert len(item["title"]) <= listings.LIMITS["title"]
        assert len(item["description"]) <= listings.LIMITS["description"]

    def test_registration_belongs_to_workshops_only(self):
        reg = {"deadline": "2026-09-12", "sessions": 4, "capacity": 12}
        assert "registration" not in listings.shape(raw(registration=reg), VENUES)
        workshop = listings.shape(raw(kind="workshop", registration=reg), VENUES)
        assert workshop["registration"] == reg

    def test_zero_means_unknown_in_registration(self):
        workshop = listings.shape(raw(kind="workshop", registration={
            "deadline": "soon", "sessions": 0, "capacity": "twelve",
        }), VENUES)
        assert "registration" not in workshop


# ---------------------------------------------------------------------------
# what the public pull request may say
# ---------------------------------------------------------------------------

class TestPublicText:
    def test_the_same_email_always_gets_the_same_branch(self):
        first = listings.proposal_ref("<abc@mail.example>")
        assert first == listings.proposal_ref("<abc@mail.example>")
        assert first != listings.proposal_ref("<def@mail.example>")
        assert listings.branch_for(first).startswith("listings/")

    def test_sender_from_the_venue_domain(self):
        assert listings.sender_is_venue("events@kgbbar.com", [VENUE]) is True
        assert listings.sender_is_venue("someone@gmail.com", [VENUE]) is False
        assert listings.sender_is_venue("someone@gmail.com", []) is None

    def test_a_lookalike_domain_is_not_the_venue(self):
        assert listings.sender_is_venue("events@kgbbar.com.evil.example", [VENUE]) is False

    def test_the_commit_never_carries_the_sender(self):
        item = listings.shape(raw(title="Reading — RSVP jane@home.net"), VENUES)
        subject, body = listings.commit_message(
            [item], received="2026-09-10 12:00 UTC", ref="abc123", sender_matches=False,
        )
        assert "jane@home.net" not in subject + body
        assert "\n" not in subject

    def test_one_listing_gets_a_descriptive_subject(self):
        subject, _ = listings.commit_message(
            [listings.shape(raw(), VENUES)], received="now", ref="r", sender_matches=True,
        )
        assert subject == "Listing: Prose night, three readers — KGB Bar, 2026-09-20"

    def test_a_series_says_so_and_asks_for_the_dates_to_be_checked(self):
        dates = ["2026-10-05", "2026-10-12", "2026-10-19"]
        series = [listings.shape(raw(title="Open mic", kind="openmic", date=day), VENUES)
                  for day in dates]
        subject, body = listings.commit_message(
            series, received="now", ref="r", sender_matches=None,
        )
        assert subject == "3 listings from KGB Bar"
        assert "recurring series" in body

    def test_an_unlisted_venue_is_named_for_the_reviewer(self):
        item = listings.shape(raw(venueId="", venueName="Cornelia Street Café"), VENUES)
        _, body = listings.commit_message([item], received="now", ref="r", sender_matches=None)
        assert "Cornelia Street Café" in body
        assert "venues.json" in body


# ---------------------------------------------------------------------------
# the git path, against a real repository standing in for GitHub
# ---------------------------------------------------------------------------

def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def glyph(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "glyph"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True)
    (work / "public" / "data").mkdir(parents=True)
    (work / "public" / "data" / "venues.json").write_text(
        json.dumps({"venues": [VENUE]}), encoding="utf-8"
    )
    git(work, "add", "-A")
    git(work, *AUTHOR, "commit", "-q", "-m", "seed")
    git(work, "push", "-q", "origin", "HEAD:main")
    return origin, work


class TestProposing:
    def test_no_checkout_means_intake_is_off(self, tmp_path):
        assert GlyphRepo.open(str(tmp_path / "nowhere")) is None
        assert GlyphRepo.open("") is None

    def test_it_reads_the_venue_registry(self, glyph):
        _, work = glyph
        assert set(GlyphRepo(str(work)).venues) == {"kgb-bar"}

    def test_a_proposal_lands_as_a_branch_with_one_file_per_listing(self, glyph):
        origin, work = glyph
        repo = GlyphRepo(str(work))
        first = listings.shape(raw(), VENUES)
        second = listings.shape(raw(date="2026-09-27"), VENUES)

        assert not repo.already_proposed("listings/abc")
        repo.propose("listings/abc", [first, second], "2 listings from KGB Bar", "body")
        assert repo.already_proposed("listings/abc")

        files = git(origin, "ls-tree", "-r", "--name-only", "listings/abc").split()
        assert "feed/curated/" + first["id"] + ".json" in files
        assert "feed/curated/" + second["id"] + ".json" in files
        assert git(origin, "log", "-1", "--format=%s", "listings/abc").strip() == \
            "2 listings from KGB Bar"
        landed = json.loads(git(origin, "show", "listings/abc:feed/curated/"
                                + first["id"] + ".json"))
        assert landed == first

    def test_the_checkout_is_clean_for_the_next_email(self, glyph):
        _, work = glyph
        repo = GlyphRepo(str(work))
        repo.propose("listings/one", [listings.shape(raw(), VENUES)], "one", "body")
        repo.propose("listings/two", [listings.shape(raw(title="Other"), VENUES)], "two", "body")

        # Back at the base commit, with nothing of either email left behind.
        files = git(work, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert not any(name.startswith("feed/curated/") for name in files)
        leftover = list((work / "feed" / "curated").glob("*.json")) \
            if (work / "feed" / "curated").exists() else []
        assert leftover == []

    def test_one_emails_branch_never_carries_anothers_listings(self, glyph):
        origin, work = glyph
        repo = GlyphRepo(str(work))
        one = listings.shape(raw(), VENUES)
        two = listings.shape(raw(title="Other"), VENUES)
        repo.propose("listings/one", [one], "one", "body")
        repo.propose("listings/two", [two], "two", "body")
        files = git(origin, "ls-tree", "-r", "--name-only", "listings/two").split()
        assert "feed/curated/" + two["id"] + ".json" in files
        assert "feed/curated/" + one["id"] + ".json" not in files
