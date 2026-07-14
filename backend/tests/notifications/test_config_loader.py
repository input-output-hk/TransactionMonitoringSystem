"""Unit tests for the DB-backed notification config loader.

Guards the sync-cache / async-refresh contract the refactor introduced: the
accessors read the in-memory cache only (no I/O), refresh_from_db seeds + caches
the default on an empty DB, and validation rejects bad documents.
"""

import pytest

from app.notifications import config


@pytest.fixture(autouse=True)
def _restore_cache():
    saved = config._config
    yield
    config._config = saved


def test_load_returns_default_when_cache_empty():
    config._config = None
    assert config.load() is config._DEFAULT_CONFIG
    assert config.channel_enabled("email") is True
    assert config.channel_enabled("webhook") is False


def test_load_returns_injected_cache():
    config._config = {"channels": {"webhook": {"enabled": True}}, "triggers": {"defaults": {}}}
    assert config.channel_enabled("webhook") is True
    assert config.channel_enabled("email") is False


def test_load_does_no_io(monkeypatch):
    """An accessor must never touch the DB — even if the DB layer would raise."""
    import app.db.postgres as pg

    async def boom():
        raise AssertionError("load() must not hit the DB")

    monkeypatch.setattr(pg, "get_notification_config", boom, raising=False)
    config._config = None
    config.load()  # must not raise
    config.channel_enabled("email")


def test_validate_accepts_default():
    config._validate("t", config._DEFAULT_CONFIG)  # no raise


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 2, "channels": {"email": {"enabled": True}}, "triggers": {"defaults": {}}},
        {"version": 1, "channels": {}, "triggers": {"defaults": {}}},
        {
            "version": 1,
            "channels": {"email": {"enabled": True}},
            "triggers": {"defaults": {"Bogus": ["email"]}},
        },
        {
            "version": 1,
            "channels": {"email": {"enabled": True}},
            "triggers": {"defaults": {"Critical": ["sms"]}},
        },
        {
            "version": 1,
            "channels": {"email": {"enabled": True, "smtp_password": "x"}},
            "triggers": {"defaults": {}},
        },
        # secret-looking keys in non-snake spellings must also be rejected
        {
            "version": 1,
            "channels": {"email": {"enabled": True, "smtpPassword": "x"}},
            "triggers": {"defaults": {}},
        },
        {
            "version": 1,
            "channels": {"email": {"enabled": True}},
            "triggers": {"defaults": {}},
            "webhookSigningSecret": "x",
        },
        {
            "version": 1,
            "channels": {"email": {"enabled": True}},
            "triggers": {"defaults": {}},
            "apiKey": "x",
        },
    ],
)
def test_validate_rejects_bad_docs(bad):
    with pytest.raises(RuntimeError):
        config._validate("t", bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000/hook",
        "http://localhost/hook",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.0.0.5/hook",
        "http://192.168.1.5/hook",
    ],
)
def test_validate_rejects_internal_webhook_url_by_default(url):
    doc = {
        "version": 1,
        "channels": {"webhook": {"enabled": True, "default_url": url}},
        "triggers": {"defaults": {}},
    }
    with pytest.raises(RuntimeError, match="SSRF"):
        config._validate("t", doc)


def test_validate_allows_internal_webhook_url_with_opt_in(monkeypatch):
    monkeypatch.setattr(config.settings, "WEBHOOK_ALLOW_INTERNAL", True)
    doc = {
        "version": 1,
        "channels": {"webhook": {"enabled": True, "default_url": "http://10.0.0.5/hook"}},
        "triggers": {"defaults": {}},
    }
    config._validate("t", doc)  # no raise


def test_validate_allows_public_webhook_url():
    doc = {
        "version": 1,
        "channels": {"webhook": {"enabled": True, "default_url": "https://hooks.example.com/x"}},
        "triggers": {"defaults": {}},
    }
    config._validate("t", doc)  # no raise


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1/x", True),
        ("http://localhost/x", True),
        ("http://169.254.169.254/x", True),
        ("http://10.1.2.3/x", True),
        ("https://hooks.example.com/x", False),
        ("", False),
    ],
)
def test_is_internal_webhook_target(url, expected):
    assert config.is_internal_webhook_target(url) is expected


@pytest.mark.asyncio
async def test_refresh_seeds_default_on_empty_db(monkeypatch):
    import app.db.postgres as pg

    seeded = {}

    async def get_none():
        return None

    async def set_cfg(doc, by):
        seeded["doc"], seeded["by"] = doc, by

    monkeypatch.setattr(pg, "get_notification_config", get_none)
    monkeypatch.setattr(pg, "set_notification_config", set_cfg)
    config._config = None

    doc = await config.refresh_from_db()
    assert doc["channels"]["email"]["enabled"] is True
    assert config._config is doc
    assert seeded["doc"]["version"] == 1  # default persisted
    assert seeded["by"] == "system:seed"


@pytest.mark.asyncio
async def test_refresh_raises_on_invalid_stored_doc(monkeypatch):
    import app.db.postgres as pg

    async def get_bad():
        return {"version": 1, "channels": {}, "triggers": {"defaults": {}}}

    monkeypatch.setattr(pg, "get_notification_config", get_bad)
    config._config = None
    with pytest.raises(RuntimeError):
        await config.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_reflects_stored_doc_in_sync_accessors(monkeypatch):
    """After refresh_from_db loads a stored doc, the SYNC accessors (called from
    the ClickHouse executor thread) reflect it immediately — the hot-reload
    contract that makes an admin edit take effect with no restart."""
    import app.db.postgres as pg

    stored = {
        "version": 1,
        "channels": {
            "email": {"enabled": False, "recipients": []},
            "webhook": {"enabled": True, "default_url": "https://hook.x/y"},
        },
        "triggers": {"defaults": {"Critical": ["webhook"]}, "rules": []},
    }

    async def get_stored():
        return stored

    monkeypatch.setattr(pg, "get_notification_config", get_stored)
    config._config = None
    await config.refresh_from_db()
    assert config.channel_enabled("webhook") is True
    assert config.channel_enabled("email") is False
    assert config.webhook_default_url() == "https://hook.x/y"
