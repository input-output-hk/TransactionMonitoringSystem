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
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _peer_is_trusted_proxy(direct: Optional[str]) -> bool:
    if direct is None:
        return False
    addr = ipaddress.ip_address(direct)
    return any(addr in net for net in settings.trusted_proxy_networks)


def client_ip(conn: Optional[HTTPConnection]) -> Optional[str]:
    """Best-effort validated client IP for a Request or WebSocket."""
    if conn is None:
        return None
    direct = parse_ip(conn.client.host if conn.client else None)
    if not settings.TRUSTED_PROXY_ENABLED or not _peer_is_trusted_proxy(direct):
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
    idx = len(entries) - settings.TRUSTED_PROXY_HOPS
    if idx < 0:
        return direct
    candidate = parse_ip(entries[idx])
    if candidate is None:
        logger.warning(
            "Malformed X-Forwarded-For entry from trusted proxy; "
            "falling back to direct peer"
        )
        return direct
    return candidate
