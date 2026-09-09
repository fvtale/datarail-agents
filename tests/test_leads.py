"""Tests for the lead store.

The store is shared by both receptionists, so the merge rules matter: the voice
agent will write into the same file the email agent does, for the same people.
"""

import json
import os

from datarail_agents.core.leads import (
    Contact,
    Interaction,
    Lead,
    LeadStore,
    normalise_email,
    normalise_phone,
)


class TestNormalisation:
    def test_email_case_and_space_do_not_matter(self):
        assert normalise_email("  Sam@Example.COM ") == "sam@example.com"

    def test_phone_formatting_does_not_matter(self):
        for written in ("718 838-9901", "(718) 838-9901", "+1 718 838 9901", "17188389901"):
            assert normalise_phone(written) == "7188389901", written

    def test_empty_values_are_safe(self):
        assert normalise_email("") == ""
        assert normalise_phone(None) == ""


class TestContactIdentity:
    def test_email_wins_over_phone(self):
        contact = Contact(email="a@b.com", phone="7188389901")
        assert contact.key() == "email:a@b.com"

    def test_phone_is_used_when_there_is_no_email(self):
        # This is the voice agent's case: a caller has a number, not an address.
        assert Contact(phone="(718) 838-9901").key() == "phone:7188389901"

    def test_same_person_written_differently_matches(self):
        assert Contact(email="Sam@Example.com").key() == Contact(email="sam@example.com").key()


class TestUpsert:
    def test_first_contact_creates_a_lead(self, tmp_path):
        store = LeadStore(str(tmp_path / "data.json"))
        lead = store.upsert("email", Contact(name="Sam", email="sam@example.com"))
        assert len(store) == 1
        assert lead.status == "new"
        assert lead.source == "email"

    def test_second_message_reuses_the_lead(self, tmp_path):
        store = LeadStore(str(tmp_path / "data.json"))
        first = store.upsert("email", Contact(email="sam@example.com"))
        second = store.upsert("email", Contact(email="sam@example.com"))
        assert first.id == second.id
        assert len(store) == 1

    def test_thread_wins_over_address(self, tmp_path):
        # Someone replying from a second address, in the same thread, is the
        # same lead -- not a new one.
        store = LeadStore(str(tmp_path / "data.json"))
        first = store.upsert("email", Contact(email="sam@work.com"), thread_id="<t1@x>")
        second = store.upsert("email", Contact(email="sam@personal.com"), thread_id="<t1@x>")
        assert first.id == second.id
        assert len(store) == 1

    def test_a_later_message_fills_blanks(self, tmp_path):
        store = LeadStore(str(tmp_path / "data.json"))
        store.upsert("email", Contact(email="sam@example.com"))
        lead = store.upsert("email", Contact(name="Sam Reed", email="sam@example.com", company="Acme"))
        assert lead.contact.name == "Sam Reed"
        assert lead.contact.company == "Acme"

    def test_a_later_message_does_not_erase_what_we_knew(self, tmp_path):
        # The important one: an unsigned follow-up must not wipe the company
        # the client gave us in their first message.
        store = LeadStore(str(tmp_path / "data.json"))
        store.upsert("email", Contact(name="Sam Reed", email="sam@example.com", company="Acme"))
        lead = store.upsert("email", Contact(email="sam@example.com"))
        assert lead.contact.name == "Sam Reed"
        assert lead.contact.company == "Acme"

    def test_voice_and_email_contacts_stay_separate_without_a_shared_key(self, tmp_path):
        store = LeadStore(str(tmp_path / "data.json"))
        store.upsert("email", Contact(email="sam@example.com"))
        store.upsert("voice", Contact(phone="7188389901"))
        assert len(store) == 2


class TestPersistence:
    def test_round_trip_preserves_everything(self, tmp_path):
        path = str(tmp_path / "data.json")
        store = LeadStore(path)
        lead = store.upsert("email", Contact(name="Sam", email="sam@example.com"), thread_id="<t1@x>")
        lead.need = "a booking site"
        lead.budget = "under 5k"
        lead.score = 50
        lead.status = "qualifying"
        lead.add_interaction(Interaction(
            at="2026-09-09T12:00:00+00:00",
            source="email",
            direction="in",
            subject="Hello",
            body="Can you build me a booking form?",
        ))
        store.save()

        reloaded = LeadStore.open(path)
        assert len(reloaded) == 1
        restored = reloaded.leads[0]
        assert restored.id == lead.id
        assert restored.contact.name == "Sam"
        assert restored.need == "a booking site"
        assert restored.budget == "under 5k"
        assert restored.status == "qualifying"
        assert restored.thread_ids == ["<t1@x>"]
        assert len(restored.interactions) == 1

    def test_missing_file_loads_as_empty(self, tmp_path):
        store = LeadStore.open(str(tmp_path / "nothing-here.json"))
        assert len(store) == 0

    def test_save_creates_missing_directories(self, tmp_path):
        path = str(tmp_path / "public" / "leads" / "data.json")
        store = LeadStore(path)
        store.upsert("email", Contact(email="sam@example.com"))
        store.save()
        assert os.path.exists(path)

    def test_saved_file_has_a_schema_version_and_count(self, tmp_path):
        path = str(tmp_path / "data.json")
        store = LeadStore(path)
        store.upsert("email", Contact(email="sam@example.com"))
        store.save()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["schema_version"] == 1
        assert payload["count"] == 1
        assert payload["leads"][0]["contact"]["email"] == "sam@example.com"

    def test_newest_lead_is_written_first(self, tmp_path):
        path = str(tmp_path / "data.json")
        store = LeadStore(path)
        older = store.upsert("email", Contact(email="old@example.com"))
        newer = store.upsert("email", Contact(email="new@example.com"))
        older.updated_at = "2026-01-01T00:00:00+00:00"
        newer.updated_at = "2026-09-09T00:00:00+00:00"
        store.save()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["leads"][0]["contact"]["email"] == "new@example.com"


class TestRateLimitSupport:
    def test_sends_since_counts_only_outbound(self):
        lead = Lead.new("email", Contact(email="sam@example.com"))
        lead.add_interaction(Interaction(at="2026-09-09T10:00:00+00:00", source="email", direction="in"))
        lead.add_interaction(Interaction(at="2026-09-09T11:00:00+00:00", source="email", direction="out"))
        lead.add_interaction(Interaction(at="2026-09-09T12:00:00+00:00", source="email", direction="out"))
        assert lead.sends_since("2026-09-09T00:00:00+00:00") == 2

    def test_sends_since_ignores_older_messages(self):
        lead = Lead.new("email", Contact(email="sam@example.com"))
        lead.add_interaction(Interaction(at="2026-01-01T00:00:00+00:00", source="email", direction="out"))
        assert lead.sends_since("2026-09-09T00:00:00+00:00") == 0

    def test_sends_since_works_after_a_round_trip(self, tmp_path):
        # Interactions come back from JSON as dicts, not Interaction objects.
        # The rate limit has to keep working across that boundary, because on
        # Actions every run reads the file cold.
        path = str(tmp_path / "data.json")
        store = LeadStore(path)
        lead = store.upsert("email", Contact(email="sam@example.com"))
        lead.add_interaction(Interaction(
            at="2026-09-09T11:00:00+00:00", source="email", direction="out"
        ))
        store.save()

        reloaded = LeadStore.open(path)
        assert reloaded.leads[0].sends_since("2026-09-09T00:00:00+00:00") == 1
