"""Tests for reading configuration from the environment.

GitHub Actions passes an unset repository variable as an empty string. The
workflow forwards every optional setting as `${{ vars.X }}`, so with the
variables left unset -- which the README says is fine -- every one of them
arrives as "". These tests pin that "" means "use the default", because until
they existed it meant a blank mailbox address and a crash on the first run.
"""

import pytest

from datarail_agents.core.config import Config, ConfigError

OPTIONAL = (
    "DATARAIL_MAIL_ADDRESS", "DATARAIL_MAIL_USERNAME", "OPENAI_MODEL",
    "OPENAI_CLASSIFIER_MODEL", "OPENAI_TIMEOUT", "DATARAIL_MAX_SENDS_PER_RUN",
    "DATARAIL_MAX_SENDS_PER_THREAD_PER_DAY", "DATARAIL_MAX_LISTINGS_PER_RUN",
    "DATARAIL_DISCLOSE_AGENT", "DATARAIL_AGENT_DRY_RUN", "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_CALENDAR_ID", "DATARAIL_TIMEZONE", "DATARAIL_SITE_ROOT",
)


@pytest.fixture
def actions_env(monkeypatch):
    """The environment the workflow creates when no variables are set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DATARAIL_MAIL_PASSWORD", "secret")
    for name in OPTIONAL:
        monkeypatch.setenv(name, "")
    return monkeypatch


def test_empty_variables_fall_back_to_their_defaults(actions_env):
    config = Config.load()
    assert config.mailbox.address == "contact@datarail.org"
    assert config.mailbox.username == "contact@datarail.org"
    assert config.brain.model == "gpt-5"
    assert config.brain.classifier_model == "gpt-5-mini"
    assert config.max_sends_per_run == 10
    assert config.max_sends_per_thread_per_day == 2
    assert config.max_listings_per_run == 5
    assert config.site_root == "site"


def test_empty_switches_stay_safe(actions_env):
    config = Config.load()
    # Dry run on, disclosure on, and no calendar -- the safe reading of nothing.
    assert config.dry_run is True
    assert config.disclose_agent is True
    assert config.calendar is None


def test_a_variable_that_is_set_still_wins(actions_env):
    actions_env.setenv("OPENAI_MODEL", "gpt-something-else")
    actions_env.setenv("DATARAIL_MAX_LISTINGS_PER_RUN", "2")
    config = Config.load()
    assert config.brain.model == "gpt-something-else"
    assert config.max_listings_per_run == 2


def test_an_empty_secret_is_still_an_error(actions_env):
    actions_env.setenv("OPENAI_API_KEY", "")
    with pytest.raises(ConfigError):
        Config.load()
