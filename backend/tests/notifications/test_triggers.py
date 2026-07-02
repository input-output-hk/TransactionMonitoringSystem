"""Trigger-rule resolution matrix (spec 8.2 + 8.3).

resolve_dispatch is a pure function of the loaded config, so each test injects
a config dict into the module cache (config._config) and asserts the resolved
Dispatch list — no file or DB needed.
"""

import pytest

from app.notifications import config, triggers


BASE = {
    "channels": {
        "email": {"enabled": True, "recipients": ["ops@x.com", "group:soc"]},
        "webhook": {"enabled": True, "default_url": "https://hook.x/default"},
    },
    "groups": {"soc": ["alice@x.com", "bob@x.com"]},
    "triggers": {
        "defaults": {
            "Critical": ["email", "webhook"],
            "High": ["email"],
            "Moderate": [],
            "Informational": [],
        },
        "rules": [
            {
                "band": "High",
                "attack_classes": ["token_dust", "large_datum"],
                "channels": ["webhook"],
            },
            {
                "band": "Critical",
                "attack_classes": ["front_running"],
                "channels": ["email", "webhook"],
                "recipients": {"email": ["group:soc", "ciso@x.com"]},
                "webhook_url": "https://hook.x/critical",
            },
        ],
    },
}


@pytest.fixture
def use_config():
    saved = config._config

    def _apply(cfg):
        config._config = cfg

    yield _apply
    config._config = saved


def _by_channel(dispatches):
    return {d.channel: d for d in dispatches}


def test_informational_no_channels_is_empty(use_config):
    # Informational -> [] in BASE, so nothing dispatches (the default).
    use_config(BASE)
    assert triggers.resolve_dispatch("Informational", "phishing") == []


def test_informational_pages_when_configured(use_config):
    # An operator may route Informational (e.g. for diagnostics); the config
    # decides, not a hardcoded rule.
    cfg = {
        **BASE,
        "triggers": {"defaults": {"Informational": ["webhook"]}, "rules": []},
    }
    use_config(cfg)
    out = triggers.resolve_dispatch("Informational", "phishing")
    assert set(_by_channel(out)) == {"webhook"}
    assert _by_channel(out)["webhook"].webhook_url == "https://hook.x/default"


def test_moderate_sends_nothing_outbound(use_config):
    # Moderate -> [] by default; nothing dispatches (it stays in-app only).
    use_config(BASE)
    assert triggers.resolve_dispatch("Moderate", "phishing") == []


def test_band_default_when_no_rule_matches(use_config):
    # High + phishing matches no rule -> defaults[High] = [email].
    use_config(BASE)
    out = triggers.resolve_dispatch("High", "phishing")
    by = _by_channel(out)
    assert set(by) == {"email"}
    assert by["email"].recipients == ["ops@x.com", "alice@x.com", "bob@x.com"]
    assert by["email"].webhook_url is None


def test_unknown_channel_in_trigger_is_dropped(use_config):
    # A stale / misspelled channel name resolves to no channel and is dropped.
    cfg = {**BASE, "triggers": {"defaults": {"Critical": ["email", "ghost"]}, "rules": []}}
    use_config(cfg)
    out = triggers.resolve_dispatch("Critical", "phishing")
    assert set(_by_channel(out)) == {"email"}


def test_per_class_rule_overrides_band_default(use_config):
    # High + token_dust matches the first rule -> [webhook] only (not email).
    use_config(BASE)
    out = triggers.resolve_dispatch("High", "token_dust")
    by = _by_channel(out)
    assert set(by) == {"webhook"}
    assert by["webhook"].webhook_url == "https://hook.x/default"
    assert by["webhook"].recipients == []  # webhook has no recipient list


def test_per_rule_recipient_and_url_override(use_config):
    # Critical + front_running -> email (overridden recipients) + webhook
    # (overridden URL).
    use_config(BASE)
    out = triggers.resolve_dispatch("Critical", "front_running")
    by = _by_channel(out)
    assert set(by) == {"email", "webhook"}
    assert by["email"].recipients == ["alice@x.com", "bob@x.com", "ciso@x.com"]
    assert by["webhook"].webhook_url == "https://hook.x/critical"


def test_critical_default_uses_global_recipients_and_default_url(use_config):
    # Critical + phishing matches no rule -> defaults[Critical].
    use_config(BASE)
    out = triggers.resolve_dispatch("Critical", "phishing")
    by = _by_channel(out)
    assert set(by) == {"email", "webhook"}
    assert by["email"].recipients == ["ops@x.com", "alice@x.com", "bob@x.com"]
    assert by["webhook"].webhook_url == "https://hook.x/default"


def test_disabled_channel_is_dropped(use_config):
    cfg = {
        **BASE,
        "channels": {
            "email": {"enabled": True, "recipients": ["ops@x.com"]},
            "webhook": {"enabled": False, "default_url": "https://hook.x/default"},
        },
    }
    use_config(cfg)
    out = triggers.resolve_dispatch("Critical", "phishing")
    assert set(_by_channel(out)) == {"email"}  # webhook disabled


def test_channel_with_no_target_is_dropped(use_config):
    # webhook enabled but no URL configured anywhere -> dropped (config gap).
    cfg = {
        **BASE,
        "channels": {
            "email": {"enabled": True, "recipients": ["ops@x.com"]},
            "webhook": {"enabled": True, "default_url": ""},
        },
    }
    use_config(cfg)
    out = triggers.resolve_dispatch("Critical", "phishing")
    assert set(_by_channel(out)) == {"email"}  # webhook has no URL -> dropped


def test_email_with_no_recipients_is_dropped(use_config):
    cfg = {
        **BASE,
        "channels": {
            "email": {"enabled": True, "recipients": []},
            "webhook": {"enabled": True, "default_url": "https://hook.x/default"},
        },
    }
    use_config(cfg)
    out = triggers.resolve_dispatch("Critical", "phishing")
    assert set(_by_channel(out)) == {"webhook"}  # email has no recipients -> dropped


def test_present_but_empty_email_override_suppresses_email(use_config):
    # A rule that explicitly sets recipients.email = [] means "to nobody" for
    # email (the override is PRESENT) -> email is dropped. Distinct from OMITTING
    # the key (next test), which falls back to the channel's global recipients.
    cfg = {
        **BASE,
        "triggers": {
            "defaults": {"Critical": ["email", "webhook"]},
            "rules": [
                {
                    "band": "Critical",
                    "attack_classes": ["phishing"],
                    "channels": ["email", "webhook"],
                    "recipients": {"email": []},
                }
            ],
        },
    }
    use_config(cfg)
    out = triggers.resolve_dispatch("Critical", "phishing")
    assert set(_by_channel(out)) == {"webhook"}  # email override empty -> dropped


def test_absent_email_override_falls_back_to_global(use_config):
    # A rule that omits `recipients` entirely (what the UI produces when an
    # override is cleared) falls back to the channel's global recipient list.
    cfg = {
        **BASE,
        "triggers": {
            "defaults": {"Critical": ["email"]},
            "rules": [
                {
                    "band": "Critical",
                    "attack_classes": ["phishing"],
                    "channels": ["email"],
                }
            ],
        },
    }
    use_config(cfg)
    by = _by_channel(triggers.resolve_dispatch("Critical", "phishing"))
    assert set(by) == {"email"}
    assert by["email"].recipients == ["ops@x.com", "alice@x.com", "bob@x.com"]
