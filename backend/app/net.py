"""Validated client-IP resolution shared by rate limiting, audit, and WS.

Forwarded headers are attacker-writable on the left: a reverse proxy APPENDS
the real peer to the right of whatever the client sent, so trusting the
first (leftmost) X-Forwarded-For entry let an attacker rotate spoofed
identities past the rate limiter and forge the audit trail's source IP
(review finding). The rules here are:

- forwarded headers count only when TRUSTED_PROXY_ENABLED is on AND the
  direct TCP peer is one of the configured proxy CIDRs;
- the client is the TRUSTED_PROXY_HOPS-th entry from the RIGHT;
- an edge-set single-value header (e.g. CF-Connecting-IP) wins when
  configured, since the edge overwrites it per-request;
- every result is validated with ipaddress and degrades to the direct
  peer (or None), so a malformed header can never reach downstream
  consumers such as the audit row's ::inet cast.
"""

import ipaddress
import logging
from typing import Optional

from starlette.requests import HTTPConnection

from app.config import settings

logger = logging.getLogger(__name__)


def parse_ip(value: Optional[str]) -> Optional[str]:
    """Canonical IP string, or None for anything that is not a plain IP.

    Tolerates the ``host:port`` / ``[v6]:port`` forms some proxies emit.
    Never raises.
    """
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("[") and "]" in candidate:
        # Bracketed IPv6, optionally with a port: [::1]:443
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:
        # IPv4 with a port: 1.2.3.4:8080 (a bare IPv6 has >= 2 colons)
        candidate = candidate.split(":")[0]
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    # Dual-stack listeners (e.g. uvicorn on a Docker bridge) report IPv4
    # peers as IPv4-mapped IPv6 (::ffff:172.18.0.1). An IPv6Address never
    # matches an IPv4 trusted-proxy CIDR, which would silently disable
    # proxy trust and collapse every client into the proxy's rate-limit
    # bucket / audit IP, so unwrap to the underlying IPv4 address.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return str(addr)


# Warn only once per process when TRUSTED_PROXY_CIDRS fails to parse at
# request time: startup validation (main._validate_startup_settings) is the
# real gate, this flag just keeps a bad value that slipped past it from
# spamming a warning on every request.
_warned_malformed_cidrs = False


def _peer_is_trusted_proxy(direct: Optional[str]) -> bool:
    global _warned_malformed_cidrs
    if direct is None:
        return False
    try:
        networks = settings.trusted_proxy_networks
    except ValueError as exc:
        # Never 500 a request over a config typo: degrade to "untrusted
        # peer" (direct-IP behaviour), matching this module's never-raises
        # contract. Startup validation already refuses to boot on this.
        if not _warned_malformed_cidrs:
            logger.warning(
                "TRUSTED_PROXY_CIDRS is malformed (%s); treating all peers "
                "as untrusted until the configuration is fixed",
                exc,
            )
            _warned_malformed_cidrs = True
        return False
    addr = ipaddress.ip_address(direct)
    return any(addr in net for net in networks)


def is_trusted_proxy_peer(conn: Optional[HTTPConnection]) -> bool:
    """True if ``conn``'s direct TCP peer is a configured trusted proxy
    (``TRUSTED_PROXY_ENABLED`` and inside ``TRUSTED_PROXY_CIDRS``).

    Shared by :func:`client_ip` (trusts forwarded client-IP headers) and
    ``app.api.auth._is_secure_request`` (trusts the forwarded scheme for the
    session cookie's ``Secure`` flag) so both honour the same "a reverse
    proxy is provably in front of us" gate, rather than trusting a
    client-writable header from anyone who can reach the app directly.
    """
    if conn is None:
        return False
    direct = parse_ip(conn.client.host if conn.client else None)
    return settings.TRUSTED_PROXY_ENABLED and _peer_is_trusted_proxy(direct)


def client_ip(conn: Optional[HTTPConnection]) -> Optional[str]:
    """Best-effort validated client IP for a Request or WebSocket."""
    if conn is None:
        return None
    direct = parse_ip(conn.client.host if conn.client else None)
    if not is_trusted_proxy_peer(conn):
        return direct

    header = settings.TRUSTED_PROXY_CLIENT_IP_HEADER.strip()
    if header:
        candidate = parse_ip(conn.headers.get(header))
        if candidate is not None:
            return candidate

    entries = [
        e.strip()
        for value in conn.headers.getlist("x-forwarded-for")
        for e in value.split(",")
        if e.strip()
    ]
    # Settings enforce HOPS >= 1 (pydantic ge=1), so idx < len(entries) is
    # guaranteed there; the full-range check is defence in depth so that NO
    # hops value (e.g. a monkeypatched 0 or negative) can ever index out of
    # bounds and break the never-raises contract.
    idx = len(entries) - settings.TRUSTED_PROXY_HOPS
    if idx < 0 or idx >= len(entries):
        return direct
    candidate = parse_ip(entries[idx])
    if candidate is None:
        logger.warning(
            "Malformed X-Forwarded-For entry from trusted proxy; falling back to direct peer"
        )
        return direct
    return candidate
