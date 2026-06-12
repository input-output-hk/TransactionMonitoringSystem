"""Trusted client-IP resolution (app.net).

A reverse proxy APPENDS the real peer to the right of any client-supplied
X-Forwarded-For, so the leftmost entry is attacker-writable: trusting it
let an attacker rotate rate-limit buckets and forge the audit IP (review
finding). These tests pin the rightmost-hop rule, the trusted-peer gate,
and the never-raises validation contract.
"""

import pytest
from starlette.requests import Request

from app import net
from app.config import settings


def _request(headers=None, client=("127.0.0.1", 12345)):
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or [])
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": client,
    }
    return Request(scope)


@pytest.fixture
def proxy_enabled(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_CLIENT_IP_HEADER", "")


class TestParseIp:
    def test_plain_ipv4(self):
        assert net.parse_ip("203.0.113.7") == "203.0.113.7"

    def test_ipv4_with_port(self):
        assert net.parse_ip("203.0.113.7:8080") == "203.0.113.7"

    def test_bracketed_ipv6_with_port(self):
        assert net.parse_ip("[2001:db8::1]:443") == "2001:db8::1"

    def test_bare_ipv6(self):
        assert net.parse_ip("2001:db8::1") == "2001:db8::1"

    @pytest.mark.parametrize("garbage", [
        None, "", "not-an-ip", "evil)injection", "1.2.3.4.5", "::nope",
    ])
    def test_garbage_returns_none_never_raises(self, garbage):
        assert net.parse_ip(garbage) is None

    def test_ipv4_mapped_ipv6_unwraps_to_ipv4(self):
        # Dual-stack listeners report IPv4 peers as ::ffff:a.b.c.d; the
        # unwrap is what lets such a peer match IPv4 trusted-proxy CIDRs.
        assert net.parse_ip("::ffff:172.18.0.1") == "172.18.0.1"

    def test_ipv4_mapped_ipv6_with_port_unwraps(self):
        assert net.parse_ip("[::ffff:172.18.0.1]:443") == "172.18.0.1"

    def test_plain_ipv6_not_mangled_by_unwrap(self):
        assert net.parse_ip("2001:db8::ffff:102:304") == "2001:db8::ffff:102:304"


class TestClientIpRightmostRule:
    def test_rightmost_entry_wins(self, proxy_enabled):
        req = _request([("X-Forwarded-For", "6.6.6.6, 203.0.113.7")])
        assert net.client_ip(req) == "203.0.113.7"

    def test_spoofed_left_entries_never_win(self, proxy_enabled):
        req = _request([("X-Forwarded-For", "1.1.1.1, 2.2.2.2, 203.0.113.7")])
        assert net.client_ip(req) == "203.0.113.7"

    def test_hops_two_picks_second_from_right(self, proxy_enabled, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
        req = _request([("X-Forwarded-For", "6.6.6.6, 203.0.113.7, 10.0.0.5")])
        assert net.client_ip(req) == "203.0.113.7"

    def test_fewer_entries_than_hops_falls_back_to_peer(
        self, proxy_enabled, monkeypatch
    ):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 3)
        req = _request([("X-Forwarded-For", "203.0.113.7")])
        assert net.client_ip(req) == "127.0.0.1"

    def test_malformed_rightmost_falls_back_to_peer(self, proxy_enabled):
        req = _request([("X-Forwarded-For", "203.0.113.7, not-an-ip")])
        assert net.client_ip(req) == "127.0.0.1"

    def test_multiple_header_lines_merged(self, proxy_enabled):
        req = _request([
            ("X-Forwarded-For", "6.6.6.6"),
            ("X-Forwarded-For", "203.0.113.7"),
        ])
        assert net.client_ip(req) == "203.0.113.7"

    @pytest.mark.parametrize("hops", [1, 2, 5])
    def test_empty_xff_returns_peer_for_any_hops(
        self, proxy_enabled, monkeypatch, hops
    ):
        # Empty / whitespace-only XFF yields zero entries; no HOPS value may
        # turn that into an out-of-bounds index (never-raises contract).
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", hops)
        req = _request([("X-Forwarded-For", "  ")])
        assert net.client_ip(req) == "127.0.0.1"


class TestTrustGates:
    def test_disabled_flag_ignores_header(self):
        req = _request([("X-Forwarded-For", "6.6.6.6")])
        assert net.client_ip(req) == "127.0.0.1"

    def test_untrusted_peer_ignores_header(self, proxy_enabled):
        # A direct (non-proxy) peer must not be able to spoof via XFF.
        req = _request(
            [("X-Forwarded-For", "6.6.6.6")], client=("8.8.8.8", 443),
        )
        assert net.client_ip(req) == "8.8.8.8"

    def test_no_header_returns_peer(self, proxy_enabled):
        assert net.client_ip(_request()) == "127.0.0.1"

    def test_none_connection(self):
        assert net.client_ip(None) is None


class TestEdgeHeader:
    def test_forged_cf_header_ignored_when_not_configured(self, proxy_enabled):
        # Compose default is TRUSTED_PROXY_CLIENT_IP_HEADER empty (Cloudflare
        # is explicit opt-in). A non-Cloudflare proxy that does not strip a
        # client-forged CF-Connecting-IP must NOT have it trusted; the
        # append-safe rightmost X-Forwarded-For hop wins instead.
        req = _request([
            ("CF-Connecting-IP", "6.6.6.6"),
            ("X-Forwarded-For", "6.6.6.6, 203.0.113.7"),
        ])
        assert net.client_ip(req) == "203.0.113.7"

    def test_cf_connecting_ip_wins(self, proxy_enabled, monkeypatch):
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CLIENT_IP_HEADER", "CF-Connecting-IP",
        )
        req = _request([
            ("CF-Connecting-IP", "203.0.113.9"),
            ("X-Forwarded-For", "6.6.6.6, 1.2.3.4"),
        ])
        assert net.client_ip(req) == "203.0.113.9"

    def test_invalid_edge_header_falls_back_to_xff(
        self, proxy_enabled, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CLIENT_IP_HEADER", "CF-Connecting-IP",
        )
        req = _request([
            ("CF-Connecting-IP", "garbage"),
            ("X-Forwarded-For", "6.6.6.6, 203.0.113.7"),
        ])
        assert net.client_ip(req) == "203.0.113.7"


class TestIpv4MappedPeer:
    def test_mapped_peer_in_ipv4_cidr_is_trusted(self, proxy_enabled):
        # Dual-stack uvicorn reports the Docker bridge gateway as
        # ::ffff:172.18.0.1; without the parse_ip unwrap it would never
        # match 172.16.0.0/12 and proxy trust would silently turn off,
        # collapsing all clients into the proxy's rate bucket / audit IP.
        req = _request(
            [("X-Forwarded-For", "6.6.6.6, 203.0.113.7")],
            client=("::ffff:172.18.0.1", 443),
        )
        assert net.client_ip(req) == "203.0.113.7"

    def test_mapped_peer_outside_cidrs_stays_untrusted(
        self, proxy_enabled, monkeypatch
    ):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
        req = _request(
            [("X-Forwarded-For", "6.6.6.6")],
            client=("::ffff:172.18.0.1", 443),
        )
        # Untrusted peer: the header is ignored and the unwrapped IPv4
        # direct peer is returned.
        assert net.client_ip(req) == "172.18.0.1"


class TestMalformedCidrsAtRequestTime:
    @pytest.fixture(autouse=True)
    def _reset_warn_once(self, monkeypatch):
        monkeypatch.setattr(net, "_warned_malformed_cidrs", False)

    def test_degrades_to_direct_peer_never_raises(
        self, proxy_enabled, monkeypatch
    ):
        # Startup validation is the real gate; if a bad value still reaches
        # request time it must degrade to untrusted-peer, not 500.
        monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", "not-a-cidr")
        req = _request([("X-Forwarded-For", "6.6.6.6, 203.0.113.7")])
        assert net.client_ip(req) == "127.0.0.1"

    def test_warns_only_once(self, proxy_enabled, monkeypatch, caplog):
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8,oops",
        )
        with caplog.at_level("WARNING", logger="app.net"):
            for _ in range(3):
                assert net.client_ip(_request()) == "127.0.0.1"
        warnings = [
            r for r in caplog.records if "TRUSTED_PROXY_CIDRS" in r.getMessage()
        ]
        assert len(warnings) == 1
