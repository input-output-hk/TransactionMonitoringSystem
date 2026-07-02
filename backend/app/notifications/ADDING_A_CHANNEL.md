# Adding a new notification channel (e.g. SMS)

The notification module is built so a new delivery channel plugs in **without
touching** the dispatcher, the trigger engine, the scoring hook, or the payload
models — the "unplug Email, drop in SMS" requirement (see the module docstring
in [`channels/base.py`](channels/base.py)). This is the end-to-end recipe, using
**SMS** as the running example.

---

## How an alert flows (context)

```
engine.on_new_scores(results)                     # scoring thread
  └─ triggers.resolve_dispatch(band, attack_class) # reads the DB-backed config:
  │                                                 #   which channels fire + resolved
  │                                                 #   recipients / webhook_url
  └─ dispatcher.dispatch(payload, dispatches)       # main loop, isolated fan-out
       └─ channel.send(payload, recipients, url, attachments)   # YOUR code
```

- **Config** (channels on/off, the band×attack-class matrix, recipient lists,
  group aliases, per-rule overrides, periodic report) lives in the single
  `notification_config` Postgres row and is edited at **`/settings/notifications`**
  (admin-only). No YAML, no restart to change routing.
- **Secrets** (SMTP creds, webhook signing key, and your new SMS creds) live in
  **env only** — never in the config document.
- Channels are looked up **by name** in [`registry.py`](registry.py). The
  dispatcher, triggers, payloads, and the hook never name a concrete channel.

---

## The contract you implement

`NotificationChannel` ([`channels/base.py`](channels/base.py)):

| Member | What it is |
|---|---|
| `name: str` | machine name, e.g. `"sms"`. Must match the `registry` key and the config `channels.<name>` key. |
| `is_enabled` (property) | may this channel deliver right now? Convention: **env master switch AND `config.channel_enabled(name)`**. Evaluated at dispatch time, so a config flip takes effect with no restart. |
| `handles(payload)` (optional) | which payload types you deliver. Default: all. Override to skip e.g. the periodic report. |
| `async send(payload, recipients, target_url, attachments)` | deliver one payload. **MUST NOT raise** — return `NotificationResult(ok=False, ...)` on failure. |

`Dispatch` carries **`recipients`** (address list — email addresses, SMS phone
numbers) and **`webhook_url`** (URL-based channels). Each channel reads whichever
it needs. SMS uses `recipients`, exactly like email.

---

## Steps

### 1. Implement the channel — `channels/sms.py`

Model it on [`channels/webhook.py`](channels/webhook.py) (bounded retry, never
raises, creds from `settings`). SMS has a length limit, so render a **terse**
one-liner (unlike email's full body).

```python
"""SMS delivery channel (example).

Sends a short one-line alert via the SMS provider's HTTP API. Creds come from
env (SMS_* settings), never from the config document. Never raises — returns a
NotificationResult so one bad send can't stall the dispatcher.
"""
import logging
from typing import List, Optional

import httpx

from app.config import settings
from app.notifications import config
from app.notifications.channels.base import (
    NotificationChannel, NotificationPayload, NotificationResult,
)

logger = logging.getLogger(__name__)


class SmsChannel(NotificationChannel):
    name = "sms"

    @property
    def is_enabled(self) -> bool:
        # Two-layer gate, same as email/webhook: env master switch AND the
        # per-channel `enabled` flag in the config document.
        return settings.SMS_NOTIFY_ENABLED and config.channel_enabled("sms")

    def handles(self, payload: NotificationPayload) -> bool:
        # SMS is for urgent immediate alerts only — the periodic report is a
        # CSV-heavy digest that doesn't belong in a 160-char message.
        return getattr(payload, "notification_type", None) == "immediate_alert"

    async def send(
        self,
        payload: NotificationPayload,
        recipients: Optional[List[str]] = None,   # phone numbers
        target_url: Optional[str] = None,          # unused by SMS
        attachments=None,                          # SMS can't carry attachments
    ) -> NotificationResult:
        if not settings.SMS_AUTH_TOKEN or not settings.SMS_FROM_NUMBER:
            return NotificationResult(self.name, ok=False, skipped=True,
                                      detail="SMS transport not configured")
        if not recipients:
            return NotificationResult(self.name, ok=False, skipped=True,
                                      detail="no recipients")

        body = (
            f"[TMS {payload.risk_band}] {payload.attack_class} "
            f"{payload.risk_score:.0f}/100 tx {payload.tx_hash[:12]}…"
        )
        sent, last = 0, "no attempt"
        for number in recipients:
            try:
                async with httpx.AsyncClient(timeout=settings.SMS_TIMEOUT_SECONDS) as c:
                    resp = await c.post(
                        settings.SMS_API_URL,
                        auth=(settings.SMS_ACCOUNT_SID, settings.SMS_AUTH_TOKEN),
                        data={"From": settings.SMS_FROM_NUMBER, "To": number, "Body": body},
                    )
                if resp.status_code < 400:
                    sent += 1
                else:
                    last = f"http {resp.status_code}"
            except Exception as e:      # network / TLS / timeout — never propagate
                last = repr(e)
        if sent:
            return NotificationResult(self.name, ok=True, detail=f"sent {sent}/{len(recipients)}")
        return NotificationResult(self.name, ok=False, detail=last)
```

### 2. Register it — [`registry.py`](registry.py)

One line in `_FACTORY` — this is the only place the class name appears:

```python
_FACTORY: Dict[str, Type[NotificationChannel]] = {
    "email": EmailChannel,
    "webhook": WebhookChannel,
    "sms": SmsChannel,        # <-- add this (import SmsChannel at top)
}
```

### 3. Settings + secrets — [`../config.py`](../config.py)

Add the **env master switch** and the **transport creds**. Creds are secrets →
they live in env / `.env`, **never** in the config document:

```python
SMS_NOTIFY_ENABLED: bool = True          # master switch for SMS
SMS_API_URL: str = ""
SMS_ACCOUNT_SID: str = ""
SMS_AUTH_TOKEN: str = ""                  # secret
SMS_FROM_NUMBER: str = ""
SMS_TIMEOUT_SECONDS: int = 10
```

> ⚠️ **Do not** put `SMS_AUTH_TOKEN` (or any cred) in the config document. The
> validator's secret-key guard (`_reject_secret_keys` in
> [`config.py`](config.py)) will reject a `PUT` whose document contains any
> key that normalizes to `*token*`, `*secret*`, `*password*`, `api_key`, an
> `smtp*` prefix, etc. — returning **422**.

### 4. Make it routable in the config + UI — `_DEFAULT_CONFIG` in [`config.py`](config.py)

The channel must exist in the config document's `channels` map to appear as a
routable column in the trigger matrix. The settings page derives its columns
from `Object.keys(config.channels)` — there is **no hardcoded channel list** in
the UI, and validation (`_check_channel_list`) accepts any channel present in
`channels`. Add an SMS block, shipped **off**:

```python
"channels": {
    "email":   {"enabled": True,  "recipients": ["ops@example.com"]},
    "webhook": {"enabled": False, "default_url": ""},
    "sms":     {"enabled": False, "recipients": []},   # <-- add
},
```

Now `/settings/notifications` shows an `sms` column in "Triggers — defaults" and
in the per-class rules, and an admin can enable it, add phone-number recipients
(or a `group:` alias), and route any band/class to it — no code change.

### 5. (Optional) "configured ✓" badge — API + frontend

To surface an SMS status badge like the SMTP/webhook ones:

- [`../api/notifications_config.py`](../api/notifications_config.py): add
  `"sms_configured": bool(settings.SMS_AUTH_TOKEN)` to the `secrets_status` dict.
- `frontend/src/lib/api/notifications.ts`: add `sms_configured: boolean` to
  `SecretsStatus`.
- `frontend/src/pages/NotificationsSettingsPage.tsx`: render the badge next to
  the SMS toggle.

### 6. Tests

- Channel unit test (`backend/tests/notifications/test_sms.py`): `is_enabled`
  gating (env off / config off), `send()` happy path, `skipped` when
  unconfigured or no recipients, `ok=False` (not raise) on provider error,
  `handles()` skips the periodic report.
- Trigger routing needs **no** new test — `resolve_dispatch` is generic; the
  existing `test_triggers.py` already covers channel resolution, recipient
  overrides, and group expansion for any channel name.

---

## What you do **not** touch

`dispatcher.py`, `triggers.py`, `payloads.py`, the `on_new_scores` hook, and the
existing `email.py` / `webhook.py`. They're channel-agnostic by design. Quick
proof:

```bash
grep -rn '"sms"\|SmsChannel' backend/app/notifications | grep -v channels/sms.py
# → only registry.py (the one factory line) and config.py (_DEFAULT_CONFIG)
```

---

## Recipients vs. webhook_url

`resolve_dispatch` hands `send()` the **resolved** targets, so you don't parse
config yourself:

- Address-based channel (email, **sms**) → use `recipients` (already expanded
  from `group:` aliases and any per-rule override).
- URL-based channel (webhook) → use `target_url`.

---

## Verify end-to-end (same method as email/webhook)

1. Put `SMS_*` in `.env` and set `SMS_NOTIFY_ENABLED=true`. **Restart the
   backend** — env changes are read at startup (config changes are not; those
   hot-reload).
2. In `/settings/notifications`: enable **SMS**, add a phone recipient, tick a
   band (e.g. High) → **sms**, **Save**. (Config change = no restart.)
3. Trigger an alert — either wait for live scoring or drive the dispatch path
   directly — and confirm the provider received the message.

---

## Gotchas

- **Secrets are env-only.** A `PUT` carrying an SMS token is rejected (422).
- **`send()` must never raise** — return `NotificationResult(ok=False)`; the
  dispatcher also traps, but the channel is the first line of defense.
- **Env vs. config reload asymmetry:** `SMS_*` env needs a **restart**; enabling
  / routing SMS in the UI is **hot** (no restart, single-worker deploy).
- **UI visibility requires step 4** — a channel absent from the config
  document's `channels` map won't appear as a column, even if registered.
- **`handles()`** lets a channel opt out of payload types (SMS skips the
  periodic report here).
