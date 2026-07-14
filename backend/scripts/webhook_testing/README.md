# Webhook testing tools

Dev/test helpers for exercising the webhook notification channel
(`app/notifications/channels/webhook.py`) end to end: a local receiver that
prints what arrives, and a script that fires a sample alert through the real
send path.

Neither is part of the running system. They exist so you can watch a real
`immediate_alert` payload leave the sender and land somewhere you control.

## What's here

- `webhook_receiver.py`: a tiny stdlib-only HTTP server. Accepts POSTs, verifies
  the `X-TMS-Signature` HMAC (constant-time, as the sender mandates), pretty-prints
  the headers + JSON, and returns `200 {"ok":true}`. No dependencies, no venv:
  runs on any `python3`.
- `fire_test_alert.py`: builds one sample `ImmediateAlert` and POSTs it through the
  actual `WebhookChannel.send`, so serialization, headers, timeout, and retry are
  the production code, not a mock. Takes the destination URL as its one argument.
- `engine_emit_test.py`: fires one synthetic score through the **real engine emit
  pipeline** (`resolve_dispatch` to `dispatcher.dispatch` to `WebhookChannel.send`),
  the same routing `engine.run_once` runs after scoring a batch. Routing is
  config-driven, so it POSTs to whatever the stored config says; it takes no URL.
  Delivers to the **webhook channel only by default** so it is safe to run on the
  live server (no fake-alert emails to real recipients); `--include-all-channels`
  opts into the full `on_new_scores` path and every routed channel (WILL email).
  Needs DB access, so it must run on the host where the app runs.

## Fidelity: which tool

Three levels, cheapest to most faithful:

1. `fire_test_alert.py`: channel only. Bypasses config, the trigger matrix, dedup,
   and the dispatcher. Answers "can a webhook reach URL X".
2. `engine_emit_test.py`: the full emit pipeline with a synthetic score. Exercises
   the DB-backed trigger matrix, the enabled flag, dedup, and the dispatcher fan-out.
   Only the score dict is fake. Answers "does my routing config actually deliver".
3. Live scoring: the running engine scores a real transaction as Critical/High and
   emits it. The truest test, but you have to produce (or wait for) a genuine
   attack. Answers "does the deployed system alert end to end".

## Signing: optional

Signing is off unless a secret is set. The sender only adds the
`X-TMS-Signature` header when `WEBHOOK_SIGNING_SECRET` is non-empty; otherwise it
POSTs the JSON body unsigned and delivery works exactly the same.

For the quickest test, leave the secret unset on both sides. Set it on **both**
the sender and the receiver only when you specifically want to exercise HMAC
verification. Never set it on just one side: a receiver with a secret will flag a
missing/invalid signature when the sender is unsigned.

## Firing an alert

There is no HTTP "send test" endpoint; an alert is produced either by live
scoring or by driving the send path directly. `fire_test_alert.py` does the
latter. It needs the backend environment (venv + `.env`), so run it from the
`backend` dir:

```bash
# from backend/
python -m scripts.webhook_testing.fire_test_alert http://127.0.0.1:8001/
# or, as a file with the backend venv:
../venv/bin/python scripts/webhook_testing/fire_test_alert.py http://127.0.0.1:8001/
```

Success looks like:

```
NotificationResult(channel='webhook', ok=True, detail='http 200')
```

The sender treats **any 2xx/3xx as delivered**; a 4xx is a permanent failure (no
retry); 5xx/network errors are retried per `WEBHOOK_MAX_RETRIES`.

## Exposing the receiver to a remote sender

Pick based on where the sender runs and whether the payload may leave your infra.

### Local (sender and receiver on the same host)

```bash
python3 webhook_receiver.py --port 8001
# fire at http://127.0.0.1:8001/
```

### SSH reverse tunnel (remote sender, payload stays private)

Best when the backend runs on a remote box you can SSH to and you want to watch
output on your laptop. The tunnel binds to the server's loopback, so nothing is
publicly exposed:

```bash
# laptop: receiver on 127.0.0.1:8001, then open the tunnel
ssh -N -R 8001:localhost:8001 -o ServerAliveInterval=30 user@SERVER
```

On the server, fire at (or configure the channel's default URL to)
`http://localhost:8001/`. The config logs a loopback SSRF warning; expected and
harmless here.

### ngrok (remote sender, public HTTPS URL)

Use when you need a public URL and the payload is throwaway preprod data. ngrok
transits full payloads (tx hashes, scores) through a third party, so preprod
only, never real alerts.

```bash
# one-time: ngrok config add-authtoken <token>
python3 webhook_receiver.py --port 8001      # terminal 1
ngrok http 8001                              # terminal 2, copy the https URL
```

Then, on the server:

```bash
scp scripts/webhook_testing/fire_test_alert.py user@SERVER:~/
ssh user@SERVER
cd /path/to/backend
../venv/bin/python ~/fire_test_alert.py https://XXXX.ngrok-free.app/
```

The free ngrok URL changes on every restart, so re-pass it each session.
ngrok's browser interstitial does not affect the sender (its user-agent is
`python-httpx`, not a browser); you only see the warning if you open the URL
yourself.

## Live server behind nginx (catch real alerts)

To watch alerts arrive whenever a real Critical/High transaction fires, run the
receiver as a persistent service on the live server and route a path to it
through the existing nginx. Templates are in `deploy/`.

1. Keep the receiver up (survives reboots and crashes):

   ```bash
   sudo cp deploy/tms-webhook-test.service /etc/systemd/system/   # edit paths/user first
   sudo systemctl daemon-reload && sudo systemctl enable --now tms-webhook-test
   ```

2. Route a path to it in nginx: paste `deploy/nginx-webhook-test.conf`'s `location`
   block into the `server { }` that serves your host (change the path token), then:

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. Point the channel at that URL in the admin UI (`/settings/notifications`):
   enable **Webhook**, set default URL to
   `https://<your-dashboard-host>/webhook-test-CHANGEME/`, confirm Critical/High
   route to **webhook** (default matrix), Save.

4. Watch and wait for a real alert:

   ```bash
   journalctl -u tms-webhook-test -f
   ```

If the receiver and backend share a host and the backend is not containerized,
you can skip nginx entirely and set the default URL to `http://127.0.0.1:8001/`.
nginx earns its place when the backend runs in a container (host loopback is not
reachable from inside it) or you want to view the endpoint over TLS.

## Testing the full pipeline (config to trigger to dispatch)

`fire_test_alert.py` skips the trigger matrix by posting directly. To prove the
whole chain (config to `resolve_dispatch` to enabled-check to dedup to POST),
first configure the channel in the admin UI at `/settings/notifications`:

- enable **Webhook** and set its default URL to your receiver URL,
- make sure a band (Critical/High) routes to **webhook** (the default matrix does),
- Save (hot-reloads, no restart, no secret needed).

Then drive the real engine emit path on demand with `engine_emit_test.py` (run
from the backend dir, on the host where the app runs so it shares the DB + `.env`):

```bash
python -m scripts.webhook_testing.engine_emit_test
python -m scripts.webhook_testing.engine_emit_test --band High --score 78
```

It prints what the trigger matrix resolves for the (band, class), fires the hook
exactly as `run_once` does, and reports delivery via the dedup claim. If webhook
is not routed, it tells you what to fix and exits without firing. To go one level
further, skip this script and let a live Critical/High transaction score.
