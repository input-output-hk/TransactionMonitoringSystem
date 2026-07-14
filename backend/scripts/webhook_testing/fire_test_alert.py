"""Fire one sample immediate_alert through the REAL WebhookChannel.

Exercises the exact production send path (serialization, headers, retry) and
POSTs to the URL you pass: e.g. your ngrok https URL, an SSH-tunnel loopback
URL, or a local receiver. No DB/config needed because target_url is explicit;
signing is skipped unless WEBHOOK_SIGNING_SECRET is set in this process's env.

This is a DEV/TEST tool. It needs the backend's environment (venv + .env), so
run it from the backend dir. Either invocation works:

    # as a module (matches scripts/oneoff/*):
    python -m scripts.webhook_testing.fire_test_alert https://XXXX.ngrok-free.app/

    # or as a file with the backend venv:
    ../venv/bin/python scripts/webhook_testing/fire_test_alert.py https://XXXX.ngrok-free.app/
"""

import asyncio
import os
import sys

# Allow running as a plain file (not just `python -m`): put the backend dir
# (three levels up: webhook_testing/ -> scripts/ -> backend/) on sys.path so
# `app` imports resolve, mirroring scripts/init_db.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.notifications.channels.webhook import WebhookChannel
from app.notifications.payloads import ImmediateAlert


async def main(url: str) -> None:
    payload = ImmediateAlert(
        timestamp="2026-07-02T12:00:00Z",
        attack_class="multiple_sat",
        risk_score=91.5,
        risk_band="Critical",
        tx_hash="test-webhook-0001",
        network="preprod",
        contributing_features={"double_satisfaction": 0.98},
        baseline_source="per_script",
        dashboard_url="http://localhost:8000/attacks/test-webhook-0001",
    )
    result = await WebhookChannel().send(payload, target_url=url)
    print(result)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <webhook-url>")
    asyncio.run(main(sys.argv[1]))
