"""Locust load harness: dashboard readers over the REST API plus held-open
WebSocket feed subscribers.

Scenario shape (users, spawn rate, run time, host, subscriber count) comes
from config/performance.yaml `api_load` via perf.config.load(), applied as
argparse defaults in an init_command_line_parser hook, so a bare
`locust -f backend/perf/locustfile.py` runs the standard scenario while any
explicit CLI flag still wins (argparse only falls back to defaults for
options the command line omits).

Run via backend/perf/run_load.sh: it exports PYTHONPATH=<repo>/backend so
`perf.config` resolves, and points --csv at the shared results directory.
Auth: export TMS_PERF_API_KEY=<one of the server's API_KEYS>; the key is sent
on every request and is never hardcoded. See backend/perf/README.md for the
server-side rate-limit knobs a load run needs raised.

On test stop the harness also records a perf.results JSON artifact
(api_load.json) judging the run against the `api_load` budgets, alongside the
Locust CSV exports the report generator collates.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import random
import time
from collections import deque
from urllib.parse import urlencode, urlsplit, urlunsplit

from locust import HttpUser, User, between, events, task
from locust.exception import StopUser
from locust.runners import WorkerRunner
from locust.stats import calculate_response_time_percentile
from locust.util.timespan import parse_timespan
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

# Importing app code is safe in the harness direction (the forbidden coupling
# is app importing perf) and keeps server-owned protocol values single-sourced:
# a server-side renumbering or default change reprices the harness on import.
from app.config import Settings
from app.models.transaction import ALERT_BANDS
from app.routers.websocket import (
    WS_CLOSE_FORBIDDEN as _WS_CLOSE_FORBIDDEN,
)
from app.routers.websocket import (
    WS_CLOSE_OVERLOADED as _WS_CLOSE_OVERLOADED,
)
from app.routers.websocket import (
    WS_CLOSE_POLICY_VIOLATION as _WS_CLOSE_POLICY_VIOLATION,
)
from perf import MS_PER_SECOND as _MS_PER_SECOND
from perf import results
from perf.config import load

logger = logging.getLogger(__name__)

# The whole scenario is shaped by one config section; loading it at import
# time makes a bad YAML edit fail before any load is generated.
_API_LOAD = load().api_load

# Total users for the standard scenario: HTTP readers plus the held-open
# WebSocket subscribers, which Locust carves out of the same -u total via
# FeedSubscriber.fixed_count.
_TOTAL_DEFAULT_USERS = _API_LOAD.users + _API_LOAD.ws_subscribers

# Server-to-server auth (app/auth/api_key.py): the key rides a header whose
# name defaults to "X-API-Key" (app/config.py API_KEY_HEADER). Both are read
# from the environment so nothing secret or deployment-specific is hardcoded.
_API_KEY = os.environ.get("TMS_PERF_API_KEY", "")
_API_KEY_HEADER = os.environ.get("TMS_PERF_API_KEY_HEADER", "X-API-Key")

# Optional explicit ?network= for the read endpoints. Left empty by default:
# the API's network parameter only accepts the public network literals, so a
# run against seeded perf data relies on the server being started with
# CARDANO_NETWORK=perftest and the parameter omitted (see README).
_NETWORK = os.environ.get("TMS_PERF_NETWORK", "")

# Optional Origin header for the WebSocket upgrade. Non-browser clients send
# none and the server always allows an absent Origin, so the default is no
# header; set TMS_PERF_WS_ORIGIN to a listed origin when the target restricts
# CORS_ALLOW_ORIGINS (the /ws handshake reuses that allowlist).
_WS_ORIGIN = os.environ.get("TMS_PERF_WS_ORIGIN", "")

# Reader think time and page-size rotation come from the api_load config
# section: they shape the min_rps floor (scenario arithmetic), so they live
# next to it in config/performance.yaml and are tuned together.
_WAIT_MIN_SECONDS = _API_LOAD.think_time_min_seconds
_WAIT_MAX_SECONDS = _API_LOAD.think_time_max_seconds
_PAGE_SIZES = tuple(_API_LOAD.page_sizes)

# Detail fetches pick from hashes seen in recent list responses, like an
# operator clicking a feed row. Two full pages at the largest page size keeps
# variety without unbounded growth on long runs.
_SEEN_HASH_MAXLEN = 2 * max(_PAGE_SIZES)
_seen_hashes: deque[str] = deque(maxlen=_SEEN_HASH_MAXLEN)

# Well-formed (64 lowercase hex) hash that no real transaction carries, used
# when no list response has been seen yet; it exercises the tolerated 404
# path a stale dashboard deep link would hit.
_MISSING_TX_HASH = "0" * 64

# The alert widget filters to the canonical alerting bands; imported so a
# band-taxonomy change reprices the harness filter automatically.
_ALERT_BANDS = list(ALERT_BANDS)

# Task mix for one dashboard reader. The live feed list dominates (it is the
# landing widget and polls fastest); stats KPIs and the alerts table poll
# steadily; detail drill-downs and the older-page scroll are occasional
# clicks; /health is the rare unauthenticated probe a monitor would add.
_WEIGHT_TX_LIST = 6
_WEIGHT_TX_PAGE = 2
_WEIGHT_TX_DETAIL = 2
_WEIGHT_ANALYSIS_ALERTS = 2
_WEIGHT_ANALYSIS_RECENT = 1
_WEIGHT_STATS_SUMMARY = 2
_WEIGHT_STATS_THROUGHPUT = 2
_WEIGHT_HEALTH = 1

# WebSocket close codes for rejected handshakes are imported from
# app.routers.websocket above, so the harness can never disagree with the
# server about what a rejection looks like.

# A default-configured server throttles /ws handshakes per client IP at
# WS_HANDSHAKE_RATE_LIMIT_REQUESTS per WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS;
# the defaults are read from the app's Settings model (not restated) so a
# server-side default change reprices the stagger automatically. Every
# subscriber here shares one IP, so connects are staggered at window/limit
# padded by a safety factor (the limiter window slides, so pacing exactly at
# the limit can still clip the tail of the ramp). Operators who raise the
# server knob for load runs set TMS_PERF_WS_CONNECT_INTERVAL lower to reach
# full fan-out quickly.
_SERVER_DEFAULT_WS_HANDSHAKES_PER_WINDOW = int(
    Settings.model_fields["WS_HANDSHAKE_RATE_LIMIT_REQUESTS"].default
)
_SERVER_DEFAULT_WS_WINDOW_SECONDS = float(
    Settings.model_fields["WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS"].default
)
_WS_STAGGER_SAFETY_FACTOR = 1.25
_DEFAULT_WS_CONNECT_INTERVAL_SECONDS = (
    _SERVER_DEFAULT_WS_WINDOW_SECONDS / _SERVER_DEFAULT_WS_HANDSHAKES_PER_WINDOW
) * _WS_STAGGER_SAFETY_FACTOR
_WS_CONNECT_INTERVAL_SECONDS = float(
    os.environ.get("TMS_PERF_WS_CONNECT_INTERVAL", _DEFAULT_WS_CONNECT_INTERVAL_SECONDS)
)

# Receive-poll granularity: bounds how long a stopping subscriber blocks in
# recv() before noticing shutdown. Not a measurement knob.
_WS_RECV_TIMEOUT_SECONDS = 1.0

# Frames at or under this size are candidate pongs and get JSON-parsed for
# classification; anything larger is a broadcast by construction (the server
# pong is a ~40-byte fixed frame, broadcast events carry transaction bodies).
# If the server's pong frame ever grows past this, pongs would be miscounted
# as broadcasts; the size is deliberately generous against that.
_WS_PONG_MAX_BYTES = 128

# App-level keepalive cadence. The server answers any inbound text with a
# queued pong, so this exercises the enqueue/sender path even on a quiet
# chain; kept under the websockets library's 20 s protocol-ping default so
# the app-level path fires at least once per protocol ping cycle.
_WS_KEEPALIVE_INTERVAL_SECONDS = 15.0
_WS_KEEPALIVE_TEXT = "ping"

# Stats labels for the WebSocket pseudo-requests reported through
# events.request.fire. Broadcast/pong arrivals carry response_time=None so
# Locust counts them without polluting latency percentiles.
_WS_REQUEST_TYPE = "WS"
_WS_CONNECT_NAME = "/ws [connect]"
_WS_SESSION_NAME = "/ws [session]"
_WS_BROADCAST_NAME = "/ws [broadcast]"
_WS_PONG_NAME = "/ws [pong]"

# The read_p95 budget key names the 95th percentile of read-endpoint latency.
_READ_P95_PERCENTILE = 0.95

# Every REST task is a read; the artifact aggregates only these entries so WS
# pseudo-requests never dilute the read_p95 / min_rps judgement.
_HTTP_READ_METHOD = "GET"

# Fixed per-user RNG seeds make the filter/pagination mix repeatable across
# runs (the perf tier's deterministic-workload convention); the counter also
# hands each subscriber its connect-stagger slot.
_user_seed_counter = itertools.count()
_ws_connect_slots = itertools.count()


def _with_network(params: dict[str, object]) -> dict[str, object]:
    if _NETWORK:
        params["network"] = _NETWORK
    return params


def _ws_url(base_host: str) -> str:
    """Derive the /ws URL from the HTTP host, with the api_key query
    parameter the handshake authenticates on (browsers cannot send custom
    headers on upgrades, so the server reads the key from the query string).
    A path prefix on the host (a deployment reverse-proxied under /api) is
    preserved ahead of /ws, matching how the REST tasks resolve.
    """
    parts = urlsplit(base_host)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/") + "/ws"
    query = urlencode({"api_key": _API_KEY}) if _API_KEY else ""
    return urlunsplit((scheme, parts.netloc, path, query, ""))


@events.init_command_line_parser.add_listener
def _apply_config_defaults(parser) -> None:
    """Feed the api_load scenario in as argparse defaults: bare `locust -f`
    runs the standard scenario, TMS_PERF_* environment variables override the
    YAML (this is the seam run_load.sh documents), and explicit CLI flags
    still win over both because argparse only falls back to defaults for
    options the command line omits."""
    parser.set_defaults(
        host=os.environ.get("TMS_PERF_HOST", _API_LOAD.host),
        num_users=int(os.environ.get("TMS_PERF_USERS", _TOTAL_DEFAULT_USERS)),
        spawn_rate=float(os.environ.get("TMS_PERF_SPAWN_RATE", _API_LOAD.spawn_rate)),
        run_time=f"{int(os.environ.get('TMS_PERF_DURATION_SECONDS', _API_LOAD.duration_seconds))}s",
    )


@events.init.add_listener
def _warn_if_unauthenticated(environment, **kwargs) -> None:
    if not _API_KEY:
        logger.warning(
            "TMS_PERF_API_KEY is not set: requests go out without a key, which "
            "only works against a dev-mode server (empty API_KEYS + "
            "TMS_ALLOW_DEV_MODE=1). Keyed deployments will answer 401/4403."
        )


@events.init.add_listener
def _warn_scenario_shape(environment, **kwargs) -> None:
    """Catch scenario shapes that silently measure something other than what
    the operator asked for; warnings, not errors, because partial shapes can
    be intentional (a WS-only soak, a readers-only smoke)."""
    opts = environment.parsed_options
    if opts is None:
        return
    num_users = opts.num_users or 0
    # Locust fills fixed_count users first, so a total at or below the
    # subscriber count spawns zero DashboardReaders: read_rps records 0 and
    # the artifact fails the min_rps floor for a harness reason, not a
    # server one.
    if _API_LOAD.ws_subscribers and num_users <= _API_LOAD.ws_subscribers:
        logger.warning(
            "total users (%d) <= ws_subscribers (%d): every user becomes a "
            "WebSocket subscriber and NO dashboard readers will run. Set "
            "TMS_PERF_USERS above ws_subscribers (the default already adds "
            "them) or lower api_load.ws_subscribers.",
            num_users,
            _API_LOAD.ws_subscribers,
        )
    # The connect stagger exists for the server's per-IP handshake limiter,
    # but a full default ramp (ws_subscribers x interval) that outlives the
    # run means the advertised fan-out is never reached. Locust may have
    # already normalized run_time from "120s" to numeric seconds by init
    # time, so accept both forms.
    raw_run_time = opts.run_time
    if raw_run_time is None:
        run_time_s = None
    elif isinstance(raw_run_time, str):
        run_time_s = float(parse_timespan(raw_run_time))
    else:
        run_time_s = float(raw_run_time)
    ramp_s = _API_LOAD.ws_subscribers * _WS_CONNECT_INTERVAL_SECONDS
    if run_time_s and ramp_s >= run_time_s:
        logger.warning(
            "WebSocket connect ramp (%d subscribers x %.1fs stagger = %.0fs) "
            "meets or exceeds the %.0fs run: the last subscribers never "
            "connect. For a full fan-out raise the server's WS handshake "
            "limits and lower TMS_PERF_WS_CONNECT_INTERVAL (see README).",
            _API_LOAD.ws_subscribers,
            _WS_CONNECT_INTERVAL_SECONDS,
            ramp_s,
            run_time_s,
        )


class DashboardReader(HttpUser):
    """One operator's dashboard: polls the feed, KPIs and alert table, and
    occasionally drills into a transaction."""

    host = _API_LOAD.host
    wait_time = between(_WAIT_MIN_SECONDS, _WAIT_MAX_SECONDS)

    def on_start(self) -> None:
        self._rng = random.Random(next(_user_seed_counter))
        # Cursor for the older-page task; None until a first page seeds it.
        self._before_cursor: str | None = None
        if _API_KEY:
            self.client.headers[_API_KEY_HEADER] = _API_KEY

    def _harvest_page(self, resp) -> list[dict]:
        """Pull rows out of a ListResponse body, feeding the shared hash pool
        the detail task clicks from. Tolerates non-JSON error bodies."""
        if not resp.ok:
            return []
        try:
            rows = resp.json().get("data", [])
        except ValueError:
            return []
        for row in rows:
            tx_hash = row.get("tx_hash")
            if tx_hash:
                _seen_hashes.append(tx_hash)
        return rows

    @task(_WEIGHT_TX_LIST)
    def list_transactions(self) -> None:
        limit = self._rng.choice(_PAGE_SIZES)
        resp = self.client.get(
            "/api/v1/transactions",
            params=_with_network({"limit": limit}),
            name="/api/v1/transactions [first page]",
        )
        rows = self._harvest_page(resp)
        if rows:
            # Cursor pagination contract: pages continue strictly before the
            # oldest timestamp of the page just seen.
            self._before_cursor = rows[-1]["timestamp"]

    @task(_WEIGHT_TX_PAGE)
    def page_older_transactions(self) -> None:
        if not self._before_cursor:
            return  # nothing listed yet this session; the first-page task seeds the cursor
        limit = self._rng.choice(_PAGE_SIZES)
        resp = self.client.get(
            "/api/v1/transactions",
            params=_with_network({"limit": limit, "before": self._before_cursor}),
            name="/api/v1/transactions [older page]",
        )
        rows = self._harvest_page(resp)
        if len(rows) < limit:
            # End of history: scroll back to the top like a real reader.
            self._before_cursor = None
        elif rows:
            self._before_cursor = rows[-1]["timestamp"]

    @task(_WEIGHT_TX_DETAIL)
    def transaction_detail(self) -> None:
        tx_hash = self._rng.choice(tuple(_seen_hashes)) if _seen_hashes else _MISSING_TX_HASH
        with self.client.get(
            f"/api/v1/transactions/{tx_hash}",
            params=_with_network({}),
            name="/api/v1/transactions/[hash]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                # A rolled-back or not-yet-ingested tx behind a stale deep
                # link is an expected outcome, not a harness failure.
                resp.success()

    @task(_WEIGHT_ANALYSIS_ALERTS)
    def analysis_alerts(self) -> None:
        self.client.get(
            "/api/v1/analysis/results",
            params=_with_network(
                {
                    "risk_band": _ALERT_BANDS,
                    "sort": "score",
                    "limit": self._rng.choice(_PAGE_SIZES),
                }
            ),
            name="/api/v1/analysis/results [alerts]",
        )

    @task(_WEIGHT_ANALYSIS_RECENT)
    def analysis_recent(self) -> None:
        self.client.get(
            "/api/v1/analysis/results",
            params=_with_network({"sort": "date", "limit": self._rng.choice(_PAGE_SIZES)}),
            name="/api/v1/analysis/results [recent]",
        )

    @task(_WEIGHT_STATS_SUMMARY)
    def stats_summary(self) -> None:
        self.client.get(
            "/api/v1/transactions/stats/summary",
            params=_with_network({}),
            name="/api/v1/transactions/stats/summary",
        )

    @task(_WEIGHT_STATS_THROUGHPUT)
    def stats_throughput(self) -> None:
        # No window_minutes override: the dashboard KPI uses the server-side
        # default window.
        self.client.get(
            "/api/v1/transactions/stats/throughput",
            params=_with_network({}),
            name="/api/v1/transactions/stats/throughput",
        )

    @task(_WEIGHT_HEALTH)
    def health(self) -> None:
        self.client.get("/health")


class FeedSubscriber(User):
    """Holds one /ws connection open for the whole run, counting broadcast
    fan-out and reporting connect latency through Locust's event API."""

    host = _API_LOAD.host
    # Sized exclusively by the config: exactly this many of the -u total
    # become subscribers, the rest spawn as DashboardReader (weighted).
    fixed_count = _API_LOAD.ws_subscribers

    def on_start(self) -> None:
        self._ws = None
        self._last_keepalive = time.monotonic()
        # Slot-based stagger keeps the connect ramp under the server's
        # per-IP handshake limiter; see _WS_CONNECT_INTERVAL_SECONDS.
        delay = next(_ws_connect_slots) * _WS_CONNECT_INTERVAL_SECONDS
        if delay > 0:
            time.sleep(delay)

    def on_stop(self) -> None:
        self._close_ws()

    def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _fire_session_failure(self, message: str) -> None:
        events.request.fire(
            request_type=_WS_REQUEST_TYPE,
            name=_WS_SESSION_NAME,
            response_time=None,
            response_length=0,
            exception=RuntimeError(message),
            context={},
        )

    def _connect(self) -> None:
        url = _ws_url(self.host or _API_LOAD.host)
        started = time.perf_counter()
        try:
            self._ws = ws_connect(url, origin=_WS_ORIGIN or None)
        except Exception as exc:
            events.request.fire(
                request_type=_WS_REQUEST_TYPE,
                name=_WS_CONNECT_NAME,
                response_time=(time.perf_counter() - started) * _MS_PER_SECOND,
                response_length=0,
                exception=exc,
                context={},
            )
            time.sleep(_WS_CONNECT_INTERVAL_SECONDS)
            return
        events.request.fire(
            request_type=_WS_REQUEST_TYPE,
            name=_WS_CONNECT_NAME,
            response_time=(time.perf_counter() - started) * _MS_PER_SECOND,
            response_length=0,
            exception=None,
            context={},
        )
        self._last_keepalive = time.monotonic()

    def _handle_close(self, exc: ConnectionClosed) -> None:
        """Map the server's application close codes onto harness behaviour.

        The server accepts rejected handshakes and then closes with a coded
        frame (see app/routers/websocket.py _reject), so rejections surface
        here on the first recv, not as connect() errors.
        """
        self._close_ws()
        code = exc.rcvd.code if exc.rcvd is not None else None
        if code == _WS_CLOSE_FORBIDDEN:
            self._fire_session_failure(
                "handshake rejected 4403 (invalid/missing api_key): set TMS_PERF_API_KEY "
                "to a key in the server's API_KEYS list"
            )
            raise StopUser()
        if code == _WS_CLOSE_POLICY_VIOLATION:
            self._fire_session_failure(
                "handshake rejected 1008 (Origin not allowed): set TMS_PERF_WS_ORIGIN to an "
                "origin in the server's CORS_ALLOW_ORIGINS"
            )
            raise StopUser()
        if code == _WS_CLOSE_OVERLOADED:
            self._fire_session_failure(
                "connection closed 4429 (handshake rate limit or WS_MAX_CONNECTIONS): raise "
                "WS_HANDSHAKE_RATE_LIMIT_REQUESTS / WS_MAX_CONNECTIONS on the server for "
                "load runs, or slow the ramp via TMS_PERF_WS_CONNECT_INTERVAL"
            )
            # Sit out one full limiter window so the retry cannot re-trip it.
            time.sleep(_SERVER_DEFAULT_WS_WINDOW_SECONDS)
            return
        # Any other close (server restart, network blip): reconnect at the
        # staggered pace so a mass reconnect also respects the limiter.
        self._fire_session_failure(f"connection closed unexpectedly (code={code})")
        time.sleep(_WS_CONNECT_INTERVAL_SECONDS)

    @task
    def hold_feed(self) -> None:
        if self._ws is None:
            self._connect()
            if self._ws is None:
                return
        try:
            if time.monotonic() - self._last_keepalive >= _WS_KEEPALIVE_INTERVAL_SECONDS:
                self._ws.send(_WS_KEEPALIVE_TEXT)
                self._last_keepalive = time.monotonic()
            message = self._ws.recv(timeout=_WS_RECV_TIMEOUT_SECONDS)
        except TimeoutError:
            return  # quiet interval: nothing broadcast, keep holding
        except ConnectionClosed as exc:
            self._handle_close(exc)
            return
        # Classify without JSON-decoding broadcast payloads: the server's
        # keepalive pong is a tiny fixed frame ({"type": "pong", ...},
        # app/routers/websocket.py) while broadcasts carry transaction-sized
        # bodies. Parsing every broadcast across ws_subscribers sockets would
        # burn CPU on the shared gevent loop and contaminate the HTTP latency
        # measurement running in the same process.
        msg_type = None
        if len(message) <= _WS_PONG_MAX_BYTES:
            try:
                msg_type = json.loads(message).get("type")
            except (ValueError, AttributeError):
                msg_type = None
        events.request.fire(
            request_type=_WS_REQUEST_TYPE,
            name=_WS_PONG_NAME if msg_type == "pong" else _WS_BROADCAST_NAME,
            # Sentinel per Locust's API: None counts the event without
            # entering latency percentiles (arrivals have no request to time).
            response_time=None,
            response_length=len(message),
            exception=None,
            context={},
        )


if _API_LOAD.ws_subscribers == 0:
    # fixed_count=0 is falsy, so Locust would fall back to weight-based
    # spawning and still create subscribers; a zero budget must mean none.
    FeedSubscriber.abstract = True


@events.test_stop.add_listener
def _record_api_load_artifact(environment, **kwargs) -> None:
    """Judge the run against the api_load budgets and record the JSON
    artifact the report generator collates next to the Locust CSVs.

    read_p95 is computed over the HTTP read entries only (WS pseudo-requests
    carry no response time), and min_rps over the same entries so held-open
    sockets cannot inflate the request rate.
    """
    if isinstance(environment.runner, WorkerRunner):
        return  # workers stream stats to the master; only one artifact per run
    stats = environment.stats
    read_entries = [e for e in stats.entries.values() if e.method == _HTTP_READ_METHOD]
    num_requests = sum(e.num_requests for e in read_entries)
    num_failures = sum(e.num_failures for e in read_entries)
    merged_response_times: dict[int, int] = {}
    for entry in read_entries:
        for response_time, count in entry.response_times.items():
            merged_response_times[response_time] = (
                merged_response_times.get(response_time, 0) + count
            )
    read_p95_ms = calculate_response_time_percentile(
        merged_response_times, num_requests, _READ_P95_PERCENTILE
    )
    started = stats.total.start_time
    last = stats.total.last_request_timestamp
    duration_s = (last - started) if (started and last and last > started) else 0.0
    read_rps = (num_requests / duration_s) if duration_s else 0.0

    ws_connects = stats.entries.get((_WS_CONNECT_NAME, _WS_REQUEST_TYPE))
    ws_broadcasts = stats.entries.get((_WS_BROADCAST_NAME, _WS_REQUEST_TYPE))
    # A zero-request run needs no separate guard: read_rps is then 0.0, which
    # fails the min_rps floor by construction (and the failure ratio pins to
    # fully-failed). The failure-ratio check exists because p95 and rps alone
    # are failure-blind: fast rejections (429s from an unraised server rate
    # limit) would otherwise pass both while most traffic was refused.
    read_failure_ratio = (num_failures / num_requests) if num_requests else 1.0
    checks = [
        results.check("read_p95_ms", read_p95_ms, "<=", _API_LOAD.budgets_ms.read_p95),
        results.check("read_rps", read_rps, ">=", _API_LOAD.min_rps),
        results.check(
            "read_failure_ratio", read_failure_ratio, "<=", _API_LOAD.max_read_failure_ratio
        ),
    ]
    metrics = {
        "read_requests": num_requests,
        "read_failures": num_failures,
        "read_rps": read_rps,
        "duration_seconds": duration_s,
        "ws_connects": ws_connects.num_requests if ws_connects else 0,
        "ws_connect_failures": ws_connects.num_failures if ws_connects else 0,
        "ws_connect_p95_ms": (
            ws_connects.get_response_time_percentile(_READ_P95_PERCENTILE) if ws_connects else None
        ),
        "ws_broadcasts_received": ws_broadcasts.num_requests if ws_broadcasts else 0,
    }
    passed = all(c["passed"] for c in checks)
    path = results.record("api_load", metrics=metrics, checks=checks)
    logger.info("api_load artifact recorded at %s (passed=%s)", path, passed)
