"""SSRF-hardening tests for the webhook channel's send-time egress check.

Config validation (test_config_loader.py) blocks an internal IP literal or
``localhost`` at write time; these tests cover the complementary check here,
a hostname that only RESOLVES to an internal address at send time (DNS
rebinding, or a domain re-pointed after the config was saved).
"""

import asyncio

import pytest

from app.config import settings
from app.notifications.channels import webhook as webhook_channel
from app.notifications.channels.webhook import WebhookChannel, _resolves_internal
from app.notifications.payloads import ImmediateAlert

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    status_code = 200


class _FakeClient:
    """Stands in for httpx.AsyncClient so an "allowed" send never touches
    the network — only the SSRF gate itself is under test here."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, content=None, headers=None):
        return _FakeResponse()


def _payload() -> ImmediateAlert:
    return ImmediateAlert(
        timestamp="2026-01-01T00:00:00+00:00",
        attack_class="phishing",
        risk_score=90.0,
        risk_band="Critical",
        tx_hash="deadbeef",
        network="preprod",
        baseline_source="global_fallback",
        dashboard_url="https://dash.example.com/tx/deadbeef",
    )


def _patch_getaddrinfo(monkeypatch, ip: str) -> None:
    async def fake_getaddrinfo(host, port):
        return [(None, None, None, None, (ip, 0))]

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)


async def test_send_blocks_hostname_resolving_to_internal_address(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INTERNAL", False)
    _patch_getaddrinfo(monkeypatch, "169.254.169.254")  # cloud metadata

    channel = WebhookChannel()
    result = await channel.send(_payload(), target_url="http://attacker-controlled.example/hook")

    assert result.ok is False
    assert "internal" in result.detail


async def test_resolves_internal_true_for_private_and_link_local(monkeypatch):
    for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254"):
        _patch_getaddrinfo(monkeypatch, ip)
        assert await _resolves_internal("whatever.example") is True


async def test_resolves_internal_false_for_public_address(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")
    assert await _resolves_internal("example.com") is False


async def test_resolves_internal_false_on_lookup_failure(monkeypatch):
    async def raising_getaddrinfo(host, port):
        raise OSError("name resolution failed")

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", raising_getaddrinfo)
    assert await _resolves_internal("no-such-host.invalid") is False


async def test_send_skips_dns_check_and_delivers_when_opted_in(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INTERNAL", True)
    monkeypatch.setattr(webhook_channel.httpx, "AsyncClient", _FakeClient)

    async def boom(host, port):
        raise AssertionError("must not resolve DNS when the opt-in flag is set")

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", boom)

    channel = WebhookChannel()
    result = await channel.send(_payload(), target_url="http://internal-siem.local/hook")

    assert result.ok is True


async def test_send_delivers_when_target_resolves_publicly(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INTERNAL", False)
    monkeypatch.setattr(webhook_channel.httpx, "AsyncClient", _FakeClient)
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")

    channel = WebhookChannel()
    result = await channel.send(_payload(), target_url="http://example.com/hook")

    assert result.ok is True
