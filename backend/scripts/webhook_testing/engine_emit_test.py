"""Fire one alert through the REAL engine emit pipeline (webhook only by default).

Where fire_test_alert.py POSTs straight to a URL (channel only), this reads the
stored config and runs the real routing + dispatch the engine runs after scoring:

    resolve_dispatch(band, class)   # DB-backed trigger matrix (rules + defaults)
      -> build_immediate_alert(...)  # the real payload builder
      -> dispatcher.dispatch(...)    # enabled-check + fan-out -> WebhookChannel.send

SAFETY: by default it delivers to the **webhook channel only**, even if the band
also routes to email, so testing on the live server does NOT spam real email
recipients with a fake attack. Pass --include-all-channels to exercise the full
on_new_scores path (dedup included) and every routed channel (WILL email).

Routing is config-driven, so the destination is whatever the stored config says:
enable the webhook channel and set its default_url (admin UI, or PUT
/api/notifications/config) to your receiver URL first, and make sure the chosen
band routes to webhook. This script takes no URL.

It needs the backend environment (venv + .env) and DB access, so run it from the
backend dir on the host where the app runs (typically the server):

    python -m scripts.webhook_testing.engine_emit_test
    python -m scripts.webhook_testing.engine_emit_test --band High --score 78
    python -m scripts.webhook_testing.engine_emit_test --include-all-channels
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

# Allow running as a plain file too (see fire_test_alert.py for the rationale).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import notifications  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import postgres  # noqa: E402
from app.models.transaction import AttackClass, RiskBand  # noqa: E402
from app.notifications import dispatcher, triggers  # noqa: E402
from app.notifications.payloads import build_immediate_alert  # noqa: E402

_DEFAULT_BAND = RiskBand.CRITICAL.value          # Critical/High are the default-routed bands
_DEFAULT_CLASS = AttackClass.MULTIPLE_SAT.value  # a representative attack class
_DEFAULT_SCORE = 91.5                            # comfortably inside the Critical band for the demo

# In --include-all-channels mode the dedup claim signals completion; it is written
# only AFTER a channel delivered. Poll for it up to the dispatch ceiling + margin.
_CLAIM_POLL_INTERVAL_SECONDS = 0.25
_CLAIM_WRITE_MARGIN_SECONDS = 2.0


def _synthetic_result(tx_hash: str, network: str, band: str, attack_class: str, score: float) -> dict:
    """A score dict shaped like engine._score_transaction's output for the fields
    the notification path reads (build_immediate_alert + on_new_scores)."""
    return {
        "tx_hash": tx_hash,
        "network": network,
        "max_score": round(score, 2),
        "max_class": attack_class,
        "risk_band": band,
        "baseline_source": "per_script",
        "sub_scores": {attack_class: {"double_satisfaction": 0.98, "value_delta": 0.71}},
        "analyzed_at": datetime.now(timezone.utc),
    }


async def _run(network, band, attack_class, score, tx_hash, include_all_channels) -> int:
    # Same startup sequence as app.main.lifespan, minus ClickHouse.
    await postgres.init_pool()
    await notifications.load_config()
    notifications.set_main_loop(asyncio.get_running_loop())
    notifications.build_channels()

    dispatches = triggers.resolve_dispatch(band, attack_class)
    print(f"resolve_dispatch(band={band!r}, class={attack_class!r}) -> {dispatches or '[] (nothing routed)'}")
    if not any(d.channel == "webhook" for d in dispatches):
        print(
            "\nWebhook is NOT in the resolved dispatch. Fix one of:\n"
            "  - channels.webhook.enabled = true\n"
            "  - channels.webhook.default_url = <your receiver URL>\n"
            f"  - triggers.defaults.{band} (or a matching rule) must include 'webhook'\n"
            "    (a rule matching this class REPLACES the band default)\n"
            "via the admin UI (/settings/notifications) or PUT /api/notifications/config, "
            "then re-run.",
            file=sys.stderr,
        )
        return 1
    if not settings.WEBHOOK_NOTIFY_ENABLED:
        print(
            "\nWEBHOOK_NOTIFY_ENABLED is false in the server env: the dispatcher will "
            "SKIP webhook even though it is routed. Set it true and restart.",
            file=sys.stderr,
        )
        return 1

    result = _synthetic_result(tx_hash, network, band, attack_class, score)
    payload = build_immediate_alert(result, network)

    if include_all_channels:
        # Full fidelity: the exact hook the engine calls (dedup + every routed
        # channel). WILL email if the band routes to email.
        print(f"[all-channels] firing on_new_scores for tx {tx_hash} on {network} (this WILL email routed recipients) ...")
        await asyncio.get_running_loop().run_in_executor(
            None, notifications.on_new_scores, [result], network,
        )
        deadline = settings.NOTIFY_SEND_TIMEOUT_SECONDS + _CLAIM_WRITE_MARGIN_SECONDS
        waited = 0.0
        while waited < deadline:
            if await postgres.already_notified(network, tx_hash, band):
                print(f"delivered: dedup claim recorded for {network}/{tx_hash}@{band}")
                return 0
            await asyncio.sleep(_CLAIM_POLL_INTERVAL_SECONDS)
            waited += _CLAIM_POLL_INTERVAL_SECONDS
        print(f"no dedup claim after {deadline:.0f}s: no channel delivered.", file=sys.stderr)
        return 2

    # Default: deliver through the real dispatcher but to the webhook channel only,
    # so no email goes out. Still exercises config routing + enabled-check + send.
    webhook_only = [d for d in dispatches if d.channel == "webhook"]
    print(f"[webhook-only] dispatching to {webhook_only} for tx {tx_hash} on {network} ...")
    delivered = await dispatcher.dispatch(payload, webhook_only)
    if delivered:
        print("delivered: webhook returned < 400. Check the receiver output.")
        return 0
    print(
        "NOT delivered: the webhook POST failed. Likely a stale/unreachable "
        "default_url (check it matches your current ngrok URL) or an egress "
        "block from the server. The backend log has the exact error.",
        file=sys.stderr,
    )
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Fire one alert through the real engine emit pipeline.")
    parser.add_argument("--network", default=settings.CARDANO_NETWORK)
    parser.add_argument("--band", default=_DEFAULT_BAND, choices=[b.value for b in RiskBand])
    parser.add_argument("--attack-class", default=_DEFAULT_CLASS, choices=[c.value for c in AttackClass])
    parser.add_argument("--score", type=float, default=_DEFAULT_SCORE)
    parser.add_argument("--tx", default=None, help="tx_hash (default: unique engine-test-<uuid>)")
    parser.add_argument(
        "--include-all-channels", action="store_true",
        help="use the full on_new_scores path and deliver to EVERY routed channel (WILL email)",
    )
    args = parser.parse_args()

    tx_hash = args.tx or f"engine-test-{uuid.uuid4().hex}"
    sys.exit(asyncio.run(
        _run(args.network, args.band, args.attack_class, args.score, tx_hash, args.include_all_channels)
    ))


if __name__ == "__main__":
    main()
