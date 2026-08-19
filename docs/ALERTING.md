# Alerting: Operator Guide

This is the operator reference for the alerting deliverable: how a detection becomes an outbound notification, how to configure the routing, how to confirm that something was actually sent, and what the delivery path does and does not guarantee.

It complements two documents rather than repeating them. The environment variables that gate and tune delivery are the "Notifications delivery" table in the [RUNBOOK configuration reference](../RUNBOOK.md#additional-configuration-reference), and the webhook wire contract, the payload example, and the Python HMAC verification recipe are in [Webhook notifications: payload and signature verification](../RUNBOOK.md#webhook-notifications-payload-and-signature-verification). Read those for the values and the receiver-side contract; read this for the behaviour. Cross-references here are by heading anchor rather than line number, deliberately: line citations in a document this size go stale on the next edit to the target. Developers adding a new delivery channel should read [`backend/app/notifications/ADDING_A_CHANNEL.md`](../backend/app/notifications/ADDING_A_CHANNEL.md) instead, and [REPOSITORY-MAP.md](REPOSITORY-MAP.md#alerting) locates every file in the deliverable.

## How an Alert Happens: Two Independent Sources

There are two entirely separate paths that produce an outbound alert. They share the payload schema, the trigger configuration, the dispatcher and the deduplication table, and they share nothing else: different triggering mechanisms, different retry behaviour, and separate deduplication streams. Understanding which path a given alert came from is the first step in almost every diagnosis, because the answer to "why was this not retried?" is different for each.

### Path 1: The Per-Transaction Scorer Hook

This is the path for the nine scored attack classes (`token_dust`, `large_value`, `large_datum`, `multiple_sat`, `front_running`, `sandwich`, `circular`, `fake_token`, `phishing`).

The analysis engine polls ClickHouse for unanalyzed transactions, scores a batch, and writes the results to `tx_class_scores`. Immediately after that insert, `engine.run_once` calls `notifications.on_new_scores(results, network)` (`backend/app/analysis/engine.py:550-553`). That hook runs on the ClickHouse executor thread, which cannot `await`, so it does only fast in-memory work:

1. **Filter.** A result is skipped outright unless `risk_band` is truthy, `max_class` is truthy, and `max_score > 0`. A benign transaction scores `max_class=""` with `max_score=0` and lands in the Informational band; it is never an alert on any channel, even if an operator has deliberately routed Informational for diagnostics. This gate is in the hook, ahead of any configuration lookup, so it cannot be turned off from the UI.
2. **Route.** `triggers.resolve_dispatch(band, max_class)` reads the in-process cache of the configuration document and returns a list of `Dispatch` records, each naming a channel plus its resolved recipients or URL. An empty list means this (band, class) pair goes nowhere, and the hook moves on with no I/O.
3. **Build.** `payloads.build_immediate_alert` maps the engine result to the `immediate_alert` wire schema, including the top `NOTIFY_TOP_FEATURES` (default 5) sub-scores of the dominant class as `contributing_features`.
4. **Schedule.** The hook calls `asyncio.run_coroutine_threadsafe(_deliver_with_dedup(...), main_loop)` and **discards the future**. This is fire-and-forget by design: a slow SMTP server or a hung webhook receiver must never stall the scoring loop or hold up the analysis watermark.

Delivery then happens on the main event loop in `_deliver_with_dedup`: read the dedup ledger, acquire the delivery semaphore (`NOTIFY_MAX_CONCURRENT_DELIVERIES`, default 8), fan out through the dispatcher, and write the dedup claim only if at least one channel reported success.

The consequence of the fire-and-forget scheduling is covered in detail under [Reliability](#reliability-stated-exactly): nothing in the engine ever looks at whether that delivery succeeded.

### Path 2: The contract_anomaly Poller

`contract_anomaly` is the clustering sidecar's verdict class. It is deliberately never written to `tx_class_scores`, so it never passes through `on_new_scores` at all. Its only notification path is a dedicated poller in `backend/app/tasks/notifications.py`, started at boot only when `CLUSTERING_ENABLED` is set, and only on the leader instance.

Every `NOTIFY_CONTRACT_ANOMALY_POLL_SECONDS` (default 60) the poller does the following:

1. **Re-read everything.** `clustering_queries.flagged_for_network_async(network, raise_on_error=True)` returns every sidecar verdict row for the network whose verdict is neither `normal` nor `benign`, grouped by transaction, capped at 10,000 raw rows and ordered by reconciliation recency (`published_at DESC`, so a transaction relabelled today stays inside the cap). There is no cursor and no incremental state: the whole flagged set is re-examined on every tick. Hitting the cap is logged at WARNING, because the rows dropped beyond it are the oldest and could be missed.
2. **Resolve.** For a transaction touched by several watched contracts there is one raw row per contract. `contract_anomaly.resolve` projects each to a host-scale 0-100 score and band, and keeps the highest, so a benign verdict for one contract cannot mask an anomaly verdict for another.
3. **Route.** `resolve_dispatch(band, "contract_anomaly")`, using the same hot-reloaded configuration as the scorer path. Unrouted findings are skipped.
4. **Build, with a recall-first fallback.** If `build_contract_anomaly_alert` raises, the poller logs the exception and falls back to `build_degraded_contract_anomaly_alert`, which carries only the already-computed band and score. A payload-construction bug can degrade an alert but cannot silence one.
5. **Deliver.** The same `_deliver_with_dedup`, but with `source="contract_anomaly"`.

Two bounds keep this safe. The sidecar read is configured to **raise** rather than return an empty set, because a swallowed error on the class's only notification path would mean going silently dark on a real detection with zero observability; the exception is logged at ERROR by the loop and the tick retries in 60 seconds. And `NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK` (default 50) caps how many real send attempts one tick may make, so first enablement of the sidecar drains a backlog across ticks instead of opening thousands of simultaneous connections. A deduplicated no-op costs nothing against that budget; only an actual attempt, successful or failed, spends it.

**Which contract_anomaly findings can page at all.** The band a verdict projects to is fixed by `config/detection.yaml`. A human-labelled `malicious` cluster floors at 80 and therefore lands in Critical; an automatic `anomaly` verdict has no floor and is scaled onto `[0, 59]`, so at maximum consensus it still bands Moderate. With the default trigger matrix (Moderate silent) an automatic shape outlier cannot page on its own, and a `contract_anomaly` alert at High or Critical is by construction a human-confirmed finding. That cap is a deliberate false-positive control, documented with its mainnet evidence in the `contract_anomaly` block of `config/detection.yaml`; routing Moderate for this class is the way to see automatic verdicts, and it is noisy.

### What the Two Paths Share

Both emit the same `immediate_alert` payload schema, so a webhook receiver needs no special handling. Two fields differ by source, which is useful for receiver-side routing:

| Field | Scorer path | contract_anomaly poller |
|---|---|---|
| `attack_class` | one of the nine scored classes | always `contract_anomaly` |
| `baseline_source` | `per_script`, `per_policy` or `global_fallback` | always `global_fallback` (a consensus verdict has no per-script baseline) |
| `contributing_features` | top-N sub-scores of the dominant class | `consensus`, `iso_score`, `lof_score`, `votes` |
| `timestamp` | the score's `analyzed_at` | the verdict's `scored_at`; empty string in the degraded fallback |

Both are gated on the leader instance. The engine and the poller both run only where the Postgres session-level advisory leader lock is held; the configuration cache, the captured event loop and the channel objects, by contrast, are initialised on every instance including standbys, so a promotion needs no re-initialisation.

## The Configuration Document

Everything about routing lives in a single JSON document held as one JSONB row in the Postgres table `notification_config`. The single-row invariant is enforced by a `BOOLEAN` primary key plus `CHECK (id)`. Nothing about routing lives in a file, and there is no `notifications.yaml`; that file existed in an earlier version and was replaced by this document.

**Editing it.** Either the admin page at `/settings/notifications` in the dashboard, or `PUT /api/v1/notifications/config`. Both require an authenticated browser session whose user has `role='Admin'`; an API key alone is not sufficient. A successful `PUT` persists the document, refreshes the in-process cache immediately, and re-runs the webhook egress warning. The change takes effect on the next alert with **no restart**.

**A PUT is a whole-document replace, not a patch.** There is no `PATCH` route and no field-level merge. The dashboard reads the document, mutates it locally, and writes the whole thing back. Anything you omit from the body is either replaced by the model default or dropped. That matters most for `periodic_report`: omitting it stores no report block at all, and the runtime defaults then apply, which means the report is off.

**Validation** is a single function, `_validate` in `backend/app/notifications/config.py`, shared by the `PUT` handler, the boot-time load, and the seeding path. A rejected `PUT` returns **422** with the precise validator message in `detail`, and nothing is persisted. A stored document that fails validation at boot raises out of the lifespan and fails startup loudly, rather than failing silently at the first alert.

**On a fresh database** the safe default is seeded automatically: email enabled with the placeholder recipient `ops@example.com`, webhook disabled with an empty URL, one group alias (`soc-team`) holding the same placeholder, Critical and High routed to both channels, Moderate and Informational silent, and a `periodic_report` block that is present but disabled. That placeholder address is not a working destination. Changing it is the first thing to do on a new deployment.

### version

| Field | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | `1` | Schema version. Only `1` is understood; any other value is rejected. It exists so a future incompatible schema can be detected at load rather than misinterpreted. |

### channels

A non-empty mapping of channel name to channel specification. The keys are what the rest of the document may reference, and they are also what the dashboard derives its trigger-matrix columns from: there is no hardcoded channel list in the UI. Two channels are implemented, `email` and `webhook`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `channels.<name>.enabled` | boolean | required | The master gate inside the document. A channel that is `false` here is dropped during routing no matter what the trigger matrix says. |
| `channels.<name>.recipients` | list of strings | absent means none | Address list for address-based channels (email). An entry of the form `group:<alias>` expands to that group's members. |
| `channels.<name>.default_url` | string | `""` | Endpoint for URL-based channels (webhook). Must be `http://` or `https://`. Empty means no URL. |

Two things about `default_url` are enforced at validation time. A non-http(s) scheme is rejected, so a stored document cannot smuggle `file://` into the egress path. And a URL whose host is `localhost` (or a `.localhost` name), or an IP literal in loopback, private, link-local or reserved space, is refused unless `WEBHOOK_ALLOW_INTERNAL=true`, because the server would otherwise issue requests inside its own network on an operator-supplied URL. A hostname that merely *resolves* to an internal address is not caught here; that is re-checked by a fresh DNS lookup immediately before the request leaves.

A note on email recipients: all resolved addresses for one dispatch go into a single message's `To:` header, not `Bcc:`, so every recipient sees the whole list. If that is not acceptable, use a distribution address that fans out on the mail server side.

### groups

| Field | Type | Default | Meaning |
|---|---|---|---|
| `groups.<alias>` | list of strings | `{}` | A named recipient list. Referenced anywhere a recipient may appear as `group:<alias>`. Expansion deduplicates while preserving order. |

Referencing an undefined group is a validation error, so a renamed group cannot silently empty a recipient list.

### triggers.defaults

| Field | Type | Default | Meaning |
|---|---|---|---|
| `triggers.defaults.<band>` | list of channel names | `[]` for an omitted band | Which channels fire for any transaction in that band, absent a matching rule. |

`<band>` must be one of `Critical`, `High`, `Moderate`, `Informational`; an unknown band name fails validation rather than being silently ignored. Every channel named must exist in `channels`. An empty list, or an omitted band, means that band is silent on the outbound path. Whether a band is *visible* in the dashboard is an independent UI concern.

Any band may page if you route it, including Informational, subject to the `max_score > 0` filter described above.

### triggers.rules

An ordered list of refinements to the band defaults, scoped to specific attack classes.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `band` | string | required | One of the four bands. The rule only applies to alerts in this band. |
| `attack_classes` | list of strings | required, non-empty | Which classes the rule applies to. Valid values are the nine scored classes plus `contract_anomaly`. |
| `channels` | list of channel names | `[]` | Which channels fire when the rule matches. This **replaces** the band default; it does not add to it. An explicit `[]` therefore silences that (band, class) pair. |
| `recipients` | mapping of channel name to list of strings | absent | Per-rule recipient override. A channel present here uses this list instead of the channel's global `recipients`. A channel absent here falls back to the global list. |
| `webhook_url` | string | absent | Per-rule webhook endpoint, overriding `channels.webhook.default_url`. Same scheme and internal-address checks as the global URL. |

The distinction between an absent `recipients` entry and a present but empty one is deliberate and load-bearing: absent falls back to the channel default, present-and-empty suppresses that channel for the rule. Both behaviours are covered by tests in `backend/tests/notifications/test_triggers.py`.

The backend accepts `contract_anomaly` in `attack_classes` whether or not the sidecar is running, but the dashboard only offers it in the class picker when `clustering_enabled` is true. An already-stored value still displays, so a rule written while the sidecar was on survives turning it off.

### periodic_report

Optional. If the whole block is absent, the defaults below apply, which means the report is off.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Whether the scheduler sends anything. Checked on every tick, so toggling it takes effect without a restart. |
| `frequency` | `daily` / `weekly` / `monthly` | `weekly` | How often a report is due. The corresponding intervals are fixed spans of 1, 7 and 30 days. `monthly` is a 30-day timedelta, not a calendar month, so its send date drifts across the year. |
| `window_days` | positive integer | `7` | How far back the report window reaches. Ignored for `daily`, which always covers the preceding 24 hours. |
| `channels` | list of channel names | `["email"]` | Where the report is delivered. |
| `recipients` | list of strings | `[]` | Report-specific recipients, overriding the channel's global list. Empty means use the channel default. Group aliases are expanded. Address-based channels only: the webhook always uses `channels.webhook.default_url` for a report. |
| `attack_classes` | `"all"` or a list of class names | `"all"` | Restricts the per-class counts, the top-alerts list and the CSV to a subset. |
| `min_band` | band name | `"Moderate"` | The lowest band included in the per-class counts, the top-alerts list and the CSV. |

Note that `min_band` does not filter `alerts_by_band` or `total_transactions_scored`; those are always the full window totals, so the report shows the whole picture and then details the part worth reading.

The report resolves its own destinations directly from this block, not through `resolve_dispatch`. The trigger rules, per-rule recipient overrides and per-rule webhook URLs therefore have no effect on it.

### Secrets Are Rejected, Everywhere in the Document

The document must never carry a credential, and a recursive guard enforces that. Before any structural validation, `_reject_secret_keys` walks the entire document, including nested objects and list elements, and raises if any **key name** looks like a secret. The comparison is against a normalised form (lowercased with all non-alphanumeric characters stripped), so `smtpPassword`, `smtp_password`, `SMTP-PASSWORD`, `apiKey`, `api_key`, `webhook_signing_secret`, `authToken`, `credentials` and `privkey` are all caught by the same rule. Any key whose normalised form starts with `smtp` is rejected outright, because SMTP is configured entirely through the environment.

A `PUT` carrying such a key returns **422** and persists nothing. The message names the exact path, for example `config.channels.webhook.secret`.

The two secrets the alerting subsystem actually uses live in the environment and are read directly from settings:

| Secret | Environment variable | Used by |
|---|---|---|
| Webhook HMAC signing key | `WEBHOOK_SIGNING_SECRET` | `X-TMS-Signature` on every webhook POST. Unset means unsigned delivery, which works identically otherwise. |
| SMTP credentials and transport | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_USE_STARTTLS`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` | The email channel, shared with magic-link authentication. |

`GET /api/v1/notifications/config` returns a `secrets_status` block with `webhook_signing_secret_configured` and `smtp_configured` booleans so the dashboard can show a "configured" badge. The values themselves are never returned.

### A Worked Example

```json
{
  "version": 1,
  "channels": {
    "email": {
      "enabled": true,
      "recipients": ["group:soc-team"]
    },
    "webhook": {
      "enabled": true,
      "default_url": "https://siem.example.org/hooks/tms"
    }
  },
  "groups": {
    "soc-team": ["soc@example.org", "duty-analyst@example.org"],
    "defi-desk": ["defi-risk@example.org"]
  },
  "triggers": {
    "defaults": {
      "Critical": ["email", "webhook"],
      "High": ["webhook"],
      "Moderate": [],
      "Informational": []
    },
    "rules": [
      {
        "band": "High",
        "attack_classes": ["multiple_sat", "front_running", "sandwich"],
        "channels": ["email", "webhook"],
        "recipients": { "email": ["group:defi-desk"] }
      },
      {
        "band": "Critical",
        "attack_classes": ["contract_anomaly"],
        "channels": ["webhook"],
        "webhook_url": "https://siem.example.org/hooks/tms-clustering"
      }
    ]
  },
  "periodic_report": {
    "enabled": true,
    "frequency": "weekly",
    "window_days": 7,
    "channels": ["email"],
    "recipients": ["group:soc-team"],
    "attack_classes": "all",
    "min_band": "Moderate"
  }
}
```

Reading it as the router does:

- A **Critical `token_dust`** matches no rule, so it takes the Critical default: email to `soc@example.org` and `duty-analyst@example.org`, plus a POST to `https://siem.example.org/hooks/tms`.
- A **High `sandwich`** matches the first rule. The rule's `channels` replaces the High default, so this one goes to email as well as webhook, which the band default alone would not have done. Its email goes to `defi-risk@example.org` only: the per-rule override replaces the `soc-team` list rather than adding to it.
- A **High `token_dust`** matches no rule, so it takes the High default: webhook only.
- A **Critical `contract_anomaly`** matches the second rule, which replaces the Critical default. It goes to the clustering-specific webhook endpoint and **not** to email, because the rule's `channels` list does not include email. If you wanted email as well, the rule would have to list it explicitly. In practice only a human-labelled `malicious` cluster reaches Critical on this class, so this rule pages on confirmed findings only.
- **Moderate and Informational** send nothing outbound in any class.
- The weekly report goes by email to the `soc-team` group, covering everything at Moderate and above over the trailing seven days.

## Routing and Precedence

`triggers.resolve_dispatch(band, attack_class)` is a pure function of the cached document, which is why it is exhaustively unit-tested. It applies five rules in this order:

1. **Rule matching.** A rule matches when its `band` equals the alert's band **and** the alert's class appears in the rule's `attack_classes`. Both must hold.
2. **Last match wins.** If several rules match, the **last one in list order** is used. Earlier matches are discarded, not merged. Order in the array is therefore significant: put general rules first and specific refinements after them.
3. **A matching rule replaces the band default.** It does not extend it. The band default is consulted only when no rule matches at all. This is the single most common source of surprise: adding a rule to route one class somewhere extra will, unless the rule repeats them, remove the channels the band default was providing.
4. **Per-rule targets beat channel globals.** For each selected channel, a `recipients` entry in the matched rule replaces the channel's global `recipients`, and a rule-level `webhook_url` replaces `channels.webhook.default_url`. An entry that is absent falls back to the global. An entry that is present but empty replaces the global with nothing, which suppresses an address-based channel such as email. It does **not** suppress webhook: webhook delivers to a URL, and an empty rule-level `webhook_url` is treated as absent, so the global `default_url` still applies and the channel still fires. The only way to keep webhook out of a `(band, class)` pairing is to leave it out of that rule's `channels` list.
5. **Disabled channels are dropped before dispatch.** A channel whose `enabled` is `false`, or whose name is not in `channels` at all (a stale name left behind by an edit), is skipped during routing. No log line is emitted for this case.

After those, one more guard runs per channel: **a channel that resolves to neither recipients nor a URL is dropped, with a WARNING**:

```
notification: channel 'email' selected for band=Critical class=multiple_sat but has no
resolved recipients or URL; skipping (config gap)
```

That WARNING is the only signal that this has happened. No audit row is written, because nothing was attempted. See [Common Misconfigurations](#common-misconfigurations); this is the failure that occurred in production.

Finally, remember the pre-routing filter on the scorer path: a result is never alerted unless `risk_band` is non-empty, `max_class` is non-empty, and `max_score > 0`. A transaction that was scored and found benign is not an alert candidate at all, regardless of routing.

## Deduplication: Two Units, Two Ledgers

Suppression happens at two granularities, and it is worth knowing which one silenced a given alert.

1. **Per transaction** (`notified_alerts`): have we already told anyone about *this transaction*? No timer, purely a high-water mark of what was notified.
2. **Per group** (`notified_alert_groups`): have we recently told anyone about *this script*? This one is time-windowed, and it applies only to attack classes where repeat findings at one identity are genuinely one situation.

Both are checked before delivery, per-transaction first. Neither suppresses a *finding*: every scored transaction is written to `tx_class_scores` and visible in the dashboard regardless. What they bound is notification volume.

### Per Transaction: One Claim Per Transaction Per Source

The ledger is the Postgres table `notified_alerts`, one row per `(network, tx_hash, source)`:

| Column | Meaning |
|---|---|
| `network` | `mainnet`, `preprod` or `preview`. |
| `tx_hash` | The transaction. |
| `source` | `scorer` or `contract_anomaly`. Part of the primary key. |
| `band_rank` | The **highest** band already notified for this transaction on this source: 0 Informational, 1 Moderate, 2 High, 3 Critical. |
| `notified_at` | When the claim was last written or escalated. |

Because `band_rank` holds a high-water mark rather than a timestamp of the last send, the behaviour is:

- **A genuine escalation re-notifies.** A transaction first alerted at High and later re-scored as Critical passes the pre-check (`band_rank >= 3` is false), delivers, and updates the row to rank 3.
- **A re-score at the same or a lower band does not.** `tx_class_scores` is a `ReplacingMergeTree` and a transaction can legitimately surface as newly scored more than once, so this is what prevents the same finding paging repeatedly.
- **De-escalation is silent.** A Critical that later re-scores as High produces no notification, which is correct: the operator was already told about the worse case.

**The claim is written only after at least one channel actually delivered.** `_deliver_with_dedup` performs a read-only pre-check, then dispatches, and only calls `claim_notification` if the dispatcher reported at least one successful channel. A total delivery failure records nothing, leaving the transaction eligible to alert again. This ordering is recall-first: it prefers a possible duplicate over a silently dropped alert. The narrow TOCTOU window where two concurrent re-scores both deliver can produce a duplicate push, never a miss. It is also worth being clear that on the scorer path this "leave it eligible" property is largely theoretical, for the reason set out in the next section.

If the dedup **check** itself fails (Postgres unreachable), the code logs the exception and delivers anyway, on the same principle.

**The two sources never suppress each other.** A transaction flagged both by the per-transaction scorer and by the clustering sidecar produces one alert per source, because `source` is part of the primary key. These are two distinct detections about the same transaction and an operator should see both.

**Retention prunes only the scorer stream.** `prune_notified_alerts` runs from the housekeeping sweep every `RETENTION_SWEEP_INTERVAL_HOURS` (default 24) and deletes rows older than `NOTIFY_DEDUP_RETENTION_DAYS` (default 30) **with `source = 'scorer'`**. `contract_anomaly` rows are deliberately never pruned. The poller re-reads the entire flagged set on every tick, so deleting the claim for a still-flagged verdict would make it re-alert on the next poll, and then again every retention period, forever. A `contract_anomaly` claim's natural lifetime is the verdict's flagged lifetime: when the sidecar retracts the verdict, the poll stops surfacing it. Setting `NOTIFY_DEDUP_RETENTION_DAYS=0` disables pruning entirely and keeps everything.

### Per Group: One Alert Per Script Per Window

Per-transaction dedup answers the wrong question for some findings. A contract that holds a near-backstop datum re-spends it on every state transition, and each spend is a different `tx_hash`, so the per-transaction ledger never fires and the operator gets one page per block. Measured on mainnet 2026-07-26: one script produced 106 Critical alerts in 67 minutes, **77% of that window's entire alerting volume**, for a single situation.

Grouping collapses that to one alert per `(group, band)` per window. The ledger is `notified_alert_groups`, one row per `(network, group_key, source)`, and the `group_key` is `<attack_class>:<payment_credential>`. The credential rather than the address, so one validator reached through different stake credentials is one group.

Two properties make it a bound rather than a mute button, and both are enforced in SQL:

- **An escalation to a higher band is never suppressed.** A group that has only produced High alerts still pages on its first Critical, immediately, inside the same window. Suppression requires the recorded `band_rank` to be at least the new alert's rank.
- **Suppression expires.** A condition that persists re-alerts once per window, so a real attack cannot be silenced by an early alert. The window is `NOTIFY_GROUP_WINDOW_MINUTES`, default 60, sized so the 106-alert burst above becomes one or two pages.

Grouping is **opt-in per attack class**, and only `large_datum` is grouped today. The class list is `_GROUP_BY_EVIDENCE_KEY` in `backend/app/notifications/grouping.py`. The bound on widening it is worth stating, because it is not a general property: `phishing` findings that share a sender are **not** one situation, since each transaction is a different victim, and grouping them would hide victims two onward. A new entry needs the same kind of evidence the `large_datum` one has.

When a repeat is collapsed the application log records it at INFO, naming the group and the transaction, so the collapse is never invisible:

```
notification: collapsing <tx_hash> into the open Critical alert window for group
'large_datum:<credential>'; the finding is recorded, only the notification is
suppressed
```

Set `NOTIFY_GROUP_WINDOW_MINUTES=0` to disable grouping and restore pure per-transaction behaviour. As with the per-transaction ledger, a failed group check delivers anyway, and a claim is written only after a channel actually delivered, so a total delivery failure does not open a window that would suppress the retry.

**Why this rather than scoring these findings lower.** It is tempting to fix the volume by making the repeat findings score below the alerting bands. That was tried and reverted: it silenced three real attack shapes, because the suppression keyed on properties of the datum, and an attacker writes the datum. Volume is a delivery concern, so it is bounded in the delivery path, where it cannot cost recall. The scoring-side counterpart is `large_state_allowlist_prefixes` in `config/detection.yaml`, which is keyed on the contract's address (the one thing an attacker does not control) and is empty by default.

## Reliability, Stated Exactly

This section is deliberately conservative. The delivery path is bounded and isolated, and it is best-effort. Do not read guarantees into it that are not written here.

### Webhook: Three Attempts on Paper, Roughly One Against a Hanging Receiver

The webhook channel's own loop, with default settings:

| Quantity | Setting | Default |
|---|---|---|
| Attempts | `WEBHOOK_MAX_RETRIES + 1` | 3 |
| Per-attempt HTTP timeout | `WEBHOOK_TIMEOUT_SECONDS` | 8 s |
| Backoff after attempt *i* | `WEBHOOK_RETRY_BACKOFF_SECONDS * (i + 1)` | 1 s, then 2 s |

A `4xx` response is treated as a permanent client error and returns immediately without consuming further attempts; `5xx` responses and network, DNS, TLS and timeout errors are retried. Any status below 400 is success.

The problem is the ceiling. `dispatcher._send_one` wraps the **entire** channel send, retries and backoff included, in `asyncio.timeout(NOTIFY_SEND_TIMEOUT_SECONDS)`, which defaults to 10 seconds. The arithmetic:

```
worst case inside the channel:  8 + 1 + 8 + 2 + 8  = 27 s
dispatcher ceiling:                                  10 s
```

Against a receiver that accepts the connection and then hangs, the sequence is: attempt 1 times out at 8 s, backoff to 9 s, attempt 2 begins with 1 second of budget left, and the whole send is cancelled at 10 s. **Effectively one full attempt.** The channel raises out of the timeout, the dispatcher catches it, and it is logged as `notification channel webhook errored: TimeoutError()`. (The 27 s is the nominal figure. `WEBHOOK_TIMEOUT_SECONDS` is handed to httpx as its whole timeout configuration, which applies the value separately to connect, write, read and pool acquisition, so a pathological peer can make one attempt cost more than 8 s. It cannot make the dispatcher ceiling any softer, which is the number that matters.)

This only bites when the receiver hangs. Fast failures cost almost nothing, so a refused connection or an NXDOMAIN completes all three attempts in about 3 seconds of backoff and the retry works exactly as configured. The retry budget is real for a receiver that is down and largely fictional for a receiver that is wedged, which is the harder case.

If you want the configured retries to fit, change one of the two numbers so that the worst case fits inside the ceiling: raise `NOTIFY_SEND_TIMEOUT_SECONDS` to at least 28, or lower `WEBHOOK_TIMEOUT_SECONDS` (at 2 s the worst case is 2 + 1 + 2 + 2 + 2 = 9 s, which fits under the default 10 s ceiling). Raising the ceiling costs nothing on the scoring path, because the send is fire-and-forget on the event loop, but it does hold a slot in the `NOTIFY_MAX_CONCURRENT_DELIVERIES` semaphore for longer.

### Email: Exactly One Attempt, No Retry

The email channel calls `send_smtp` once and returns its boolean result. There is no retry loop of any kind.

Two different timeouts apply, and only one of them bounds the send as a whole. `SMTP_TIMEOUT_SECONDS` (default 10) is passed through to aiosmtplib, which applies it **per operation**: the connection attempt, and each command/response exchange, get that budget individually, so a server that answers every command slowly can keep a send alive for longer than 10 seconds in total. The hard ceiling on the whole send is the dispatcher's `NOTIFY_SEND_TIMEOUT_SECONDS` (also 10 by default), the same `asyncio.timeout` the webhook channel sits inside. Treat that as the real budget.

All recipients for one dispatch go out in a single SMTP transaction with one `To:` header. The channel reports one result for the whole set, and the result is coarser than it looks: aiosmtplib raises only when the server refuses **every** recipient, so a partial refusal (some addresses rejected at `RCPT TO`, others accepted) returns `ok=True` and is reported as delivered. Per-recipient rejections are visible in the mail server's own logs, not in TMS.

An SMTP failure is logged (`SMTP send failed: ...`) and returns `ok=False`. The message is not queued anywhere and is not tried again.

### The Scorer Path Does Not Retry, In Practice

This is the most consequential fact in this document.

`_deliver_with_dedup` is written so that a total delivery failure records no dedup claim, "so the alert is retried on the next re-score". That comment is accurate about the ledger and misleading about the outcome, because in normal operation **there is no next re-score**.

The engine's poll query, `clickhouse_scores.get_unanalyzed_transactions`, is a `LEFT ANTI JOIN` of `transactions` against `tx_class_scores` (`backend/app/db/clickhouse_scores.py:885`). A transaction is "unanalyzed" precisely as long as it has no row in `tx_class_scores`. And in `engine.run_once` the score rows are inserted **before** the alert hook runs:

```python
clickhouse.insert_class_scores(results)      # engine.py:550
notifications.on_new_scores(results, network)  # engine.py:553
```

So by the time the alert is even attempted, the transaction has already stopped satisfying the poll's predicate. The periodic full rescan uses the same anti-join with the time bound removed, so it does not resurface it either. **The engine never presents an already-scored transaction to the scorer again**, and therefore a scorer alert that fails on every channel is not re-attempted. It is lost from the outbound path.

The one real exception is a chain rollback. `clickhouse.delete_score_rows` purges `tx_class_scores` rows for rolled-back transactions specifically so a re-confirmed transaction becomes unanalyzed again; that transaction will be re-scored and can alert again. Note that its `notified_alerts` row survives the rollback, so the re-alert only fires if the new band is higher than the old one.

The offline re-score scripts under `backend/scripts/oneoff/` are not an exception. They rewrite `tx_class_scores` rows directly through `clickhouse.insert_class_scores`, never through the engine, so they neither make a transaction unanalyzed nor call the alert hook: a bulk re-score changes what the dashboard and the report show and emits nothing.

What this means operationally:

- A failed alert is **not** lost from the system. The score row exists, the transaction appears in the dashboard with its band and class, and it is counted in the periodic report. Only the push was lost.
- The compensating controls are the dashboard and the periodic report, not a retry.
- If at-least-once paging matters to you, the receiver's availability is the thing to invest in, since the sender will not compensate for it. A receiver that answers 2xx quickly and queues internally is the recommended shape, and is what the RUNBOOK's webhook contract advises.
- Watch for `notification via ... failed` and `notification channel ... errored` in the application log. Those lines are the only indication that an alert was produced and not delivered.

### The contract_anomaly Path Does Retry

The poller keeps no cursor. Every 60 seconds it re-reads the entire flagged set and re-evaluates it against the current configuration. A finding whose delivery failed recorded no claim, so the next tick tries it again, and keeps trying for as long as the sidecar still reports the verdict. A routing change also takes effect on the next tick without a restart. This is the one alert path in the system with genuine at-least-once behaviour over the lifetime of the verdict.

The cost is that the retry is bounded by the per-tick cap and by the 10,000-row fetch cap, and it is O(flagged set) work on the sidecar database every minute. Deduplication keeps the output side quiet.

### The Periodic Report Does Not Retry, and Skips the Window

The scheduler wakes every `NOTIFY_REPORT_CHECK_INTERVAL_SECONDS` (default 60). When a report is due it builds the payload and the CSV, calls `dispatcher.dispatch(...)`, and then calls `mark_report_sent(...)` **without inspecting the dispatch result**. The boundary in `notification_report_state.last_sent_at` therefore advances even when every channel failed, and the next report is a fresh trailing window. The content of the failed window is never resent.

There is one case that does not advance the boundary. If the report is enabled and due but `report_dispatches` resolves to nothing (every configured channel disabled, or with no recipients and no URL), the scheduler logs `periodic report due but no enabled channel has a recipient/URL; skipping` and returns without building or advancing. It will re-check cheaply on the next tick and fire as soon as the configuration is fixed.

## Where to Confirm a Send

There are exactly three places, and none of them is in the dashboard. There is no audit-read API and no notifications-history page.

### 1. The Application Log

The most direct evidence. Under Docker Compose:

```bash
docker compose logs -f app | grep -E "notification|Periodic report|contract_anomaly poll"
```

The lines worth knowing, with their levels:

| Level | Line | Means |
|---|---|---|
| INFO | `notification sent via webhook for 3f2a... (http 200)` | Delivered. |
| WARNING | `notification via email failed for 3f2a...: smtp send failed` | The channel was attempted and returned a failure. |
| ERROR | `notification channel webhook errored: TimeoutError()` | The channel raised or hit the dispatcher's ceiling. |
| WARNING | `notification: channel 'email' selected for band=... class=... but has no resolved recipients or URL; skipping (config gap)` | Routed but undeliverable. Nothing was attempted and **no audit row exists**. |
| WARNING | `contract_anomaly poll: per-tick alert cap (50) reached; remaining findings drain on subsequent ticks` | A backlog is draining; expect it on first enablement, investigate if it persists. |
| INFO | `Periodic report sent (window=7d, channels=['email'])` | The report was dispatched (this does not prove any channel succeeded). |
| WARNING | `periodic report due but no enabled channel has a recipient/URL; skipping` | The report is enabled and due but has nowhere to go. |
| INFO | `Notification config loaded (2 channels)` | Emitted at boot and after every admin edit; useful for confirming a hot reload happened. |

Two silent cases have no log line at all. A channel disabled in the configuration document is dropped during routing without comment. And a channel disabled by its environment switch (or with SMTP unconfigured) is dropped inside the dispatcher as `skipped`, before the logging block, so it also produces nothing. Both leave the configuration reading as though delivery were routed. A third case, a channel returning `skipped` from its own `send` (no recipients, no URL), logs at DEBUG, which is invisible at the default `LOG_LEVEL=INFO`.

Container logs are rotated (`x-logging` in `docker-compose.yml`, roughly 250 MB per service; see the RUNBOOK's log-rotation section), so the log answers "did this alert go out just now", not "what went out last quarter". For that, use the audit trail.

### 2. Postgres audit_logs

The dispatcher writes one best-effort audit row per dispatch, but **only if at least one channel produced a non-skipped result**. If every selected channel was skipped, or if routing resolved to nothing, there is no row.

```bash
docker exec -it tms-postgres sh -c \
  'exec psql -U "${POSTGRES_USER:-tms_user}" -d "${POSTGRES_DB:-tms_db}"'
```

```sql
-- Recent notification dispatches
SELECT created_at,
       entity_id                        AS tx_hash,
       details ->> 'notification_type'  AS kind,
       details ->> 'risk_band'          AS band,
       details ->> 'attack_class'       AS class,
       details ->  'sent'               AS sent,
       details ->  'failed'             AS failed
FROM audit_logs
WHERE event_type = 'notification'
ORDER BY created_at DESC
LIMIT 50;
```

`sent` and `failed` are JSON arrays of channel names, so a partial delivery (webhook succeeded, email failed) is visible as such.

```sql
-- Did anything go out for one transaction?
SELECT created_at, details -> 'sent' AS sent, details -> 'failed' AS failed
FROM audit_logs
WHERE event_type = 'notification' AND entity_id = '<tx_hash>'
ORDER BY created_at DESC;
```

**Periodic-report rows carry an empty `entity_id`.** The audit row takes its `entity_id` from the payload's `tx_hash`, and a `periodic_report` payload has no such field, so it falls back to the empty string. Filtering by transaction will therefore never show report sends. Filter on the type instead:

```sql
SELECT created_at, details -> 'sent' AS sent, details -> 'failed' AS failed
FROM audit_logs
WHERE event_type = 'notification'
  AND details ->> 'notification_type' = 'periodic_report'
ORDER BY created_at DESC;
```

Configuration edits are audited separately, as `event_type='config_change'` with `entity_type='notification_config'`:

```sql
SELECT created_at, details ->> 'actor' AS actor, ip_address
FROM audit_logs
WHERE event_type = 'config_change' AND entity_type = 'notification_config'
ORDER BY created_at DESC;
```

That records who changed the routing (the admin's email address), from where, and when. It does not record **what** changed; the previous document is not retained.

Audit rows are kept forever by default (`AUDIT_LOG_RETENTION_DAYS=0`), which is deliberate: they are the accountability record.

### 3. notified_alerts

The dedup ledger doubles as proof that a delivery succeeded, because the claim is written only after a channel reported success.

```sql
SELECT network, tx_hash, source, band_rank, notified_at
FROM notified_alerts
WHERE tx_hash = '<tx_hash>';
```

`band_rank` is 0 Informational, 1 Moderate, 2 High, 3 Critical. Two rows for one hash, one per `source`, means both the scorer and the clustering poller alerted on it.

**An absent row does not always mean nothing was sent.** If the transaction's class is grouped and an alert for the same script was already delivered inside the window, the notification was collapsed and no per-transaction claim was written. Check the group ledger before concluding a finding went unnotified:

```sql
SELECT network, group_key, source, band_rank, notified_at
FROM notified_alert_groups
WHERE group_key LIKE 'large_datum:%'
ORDER BY notified_at DESC;
```

The collapse is also logged at INFO by the delivery path, naming both the group and the collapsed transaction, so `docker compose logs app | grep collapsing` answers the same question from the other side.

```sql
-- Alerting volume by source over the last day
SELECT source, count(*)
FROM notified_alerts
WHERE notified_at > NOW() - INTERVAL '1 day'
GROUP BY source;
```

And for the report scheduler:

```sql
SELECT network, report_kind, last_sent_at, last_window_start, last_window_end
FROM notification_report_state;
```

Remember that `last_sent_at` advances on a completed send *attempt*, so it confirms the scheduler ran, not that anyone received anything. Cross-check against the audit row.

## The Periodic Report

The report is a scheduled digest rather than an alert, and it is the compensating control for everything the immediate path does not retry.

**Contents.** The payload (`notification_type: "periodic_report"`) carries:

| Field | Contents |
|---|---|
| `report_window` | `{"from": iso, "to": iso}` |
| `summary.total_transactions_scored` | All transactions scored in the window, every band. |
| `summary.alerts_by_band` | Counts for all four bands. Not filtered by `min_band`. |
| `summary.alerts_by_class` | Per-class counts, restricted to rows at or above `min_band` and to the in-scope classes. |
| `summary.false_positives_archived` | Rows added to `archived_alerts` in the window, by `archived_at`. |
| `top_alerts` | Up to `NOTIFY_REPORT_TOP_ALERTS` (default 10) transactions by score. |
| `dashboard_url` | The `/reports` page pre-scoped to the window and sorted by score. |

Every figure drawn from `tx_class_scores` excludes archived transactions, because the underlying queries default to `include_archived=False`. An analyst-archived false positive therefore leaves the band and class counts, the top-alerts list and the CSV, and appears only in `false_positives_archived`. That is what makes the report a picture of what is still believed to be dangerous rather than of everything the scorer ever emitted.

When the clustering sidecar is enabled and `contract_anomaly` is in scope, its findings are counted into `alerts_by_class` and merged into `top_alerts`, then the combined list is re-ranked and truncated. They are deliberately **not** added to `total_transactions_scored` or `alerts_by_band`, because a flagged transaction is already counted there by its nine-class score and folding it in again would double-count it. A finding counts as in-window if either the winner's `scored_at` or any of the transaction's rows' `published_at` falls in the window, so a transaction relabelled malicious during this window is not dropped just because it was originally scored earlier. If the sidecar is unreachable the report counts zero for that class and logs a warning rather than failing the whole report.

**Windowing is trailing from now, and it is not contiguous.** The scheduler asks whether `now >= last_sent_at + interval`, and when it is due the window is simply `[now - window_days, now]`. It does not resume from `last_window_end`. If the application is down for three days over a weekly boundary, the report that eventually goes out covers the seven days before it actually ran, and the days between the previous window's end and this window's start are covered by no report at all. Downtime shifts the window; it does not cause a backfill. Combined with the boundary advancing on failure, a customer who needs gapless reporting should treat the report as a convenience and the dashboard's date-ranged export as the record.

Interval and window are separate knobs, so they can disagree: `frequency: "weekly"` with `window_days: 14` sends every 7 days covering the trailing 14, giving deliberate overlap. The one coupling is that `daily` forces a 1-day window regardless of `window_days`.

**The CSV attachment.** Email deliveries carry a per-transaction CSV built to be byte-compatible with the dashboard's manual export: the same 19 columns in the same order (`tx_hash`, `analyzed_at`, `network`, `max_class`, `max_score`, `risk_band`, `fee_ada`, `output_count`, the nine `score_*` columns, `sub_scores`, `analysis_version`). Rows are fetched in pages of 1,000 and capped at **50,000**, the same hard cap the frontend export uses; a window with more qualifying rows is silently truncated at that point. Above 1,000,000 raw bytes the file is gzipped so the base64-encoded attachment stays under common SMTP size limits, arriving as `tms-report-<network>-<YYYYMMDD>.csv.gz` instead of `.csv`.

**contract_anomaly is in the body but not in the CSV.** The class is read-time-only and has no row in `tx_class_scores`, so it cannot appear in an export built from that table. It is counted in `alerts_by_class` and can appear in `top_alerts`, but a reader reconciling the summary against the attachment will find the counts do not match for that one class. This is intentional, to keep the attachment identical to the manual export.

The webhook channel receives the same JSON payload and ignores attachments entirely, so a webhook-only report has no CSV.

## Testing Delivery End to End

`backend/scripts/webhook_testing/` provides three tiers of increasing fidelity, plus a reference receiver. None of it is part of the running system.

**The receiver.** `webhook_receiver.py` is a stdlib-only HTTP server with no dependencies: it accepts POSTs, verifies the `X-TMS-Signature` HMAC with `hmac.compare_digest`, pretty-prints headers and JSON, and returns `200 {"ok":true}`. It runs on any `python3`, including on a laptop with no project checkout, and it is the worked example of the verification recipe in the RUNBOOK. It listens on `127.0.0.1:8001` unless `--host` / `--port` say otherwise.

**Tier 1: `fire_test_alert.py`, channel only.** Builds one sample `ImmediateAlert` and POSTs it through the real `WebhookChannel.send`, so serialization, headers, signing, timeout, retry and the send-time egress guard are production code rather than a mock. It bypasses the configuration document, the trigger matrix, deduplication and the dispatcher. It takes the destination URL as its single argument. This answers "can a webhook reach URL X at all".

```bash
# terminal 1
python3 backend/scripts/webhook_testing/webhook_receiver.py --port 8001

# terminal 2, from the repo root
cd backend
WEBHOOK_ALLOW_INTERNAL=true ../.venv/bin/python -m scripts.webhook_testing.fire_test_alert http://127.0.0.1:8001/
# NotificationResult(channel='webhook', ok=True, detail='http 200', skipped=False)
```

`WEBHOOK_ALLOW_INTERNAL=true` is required for a loopback target: without it the send-time DNS check refuses the request and you get `ok=False, detail='blocked: target resolves to an internal address'`, which is the guard working, not a bug. A public receiver URL needs no such flag.

**Tier 2: `engine_emit_test.py`, the full emit pipeline.** Fires one synthetic score through `resolve_dispatch` to `dispatcher.dispatch` to the channel, which is the same routing `engine.run_once` performs after scoring a batch. Only the score dictionary is fake. It exercises the DB-backed trigger matrix, the channel enabled flags (both the document flag and `WEBHOOK_NOTIFY_ENABLED`, which it checks explicitly and reports on) and the dispatcher fan-out, and it takes no URL because the destination is whatever the stored configuration says. It needs database access and the backend environment, so it must run on the host where the application runs. This answers "does my routing configuration actually deliver".

```bash
cd backend
../.venv/bin/python -m scripts.webhook_testing.engine_emit_test
../.venv/bin/python -m scripts.webhook_testing.engine_emit_test --band High --score 78
```

**It is webhook-only by default**, even when the chosen band also routes to email. That is a deliberate safety property: running it against a live production server cannot send a fake attack alert to real recipients. If webhook is not routed for the chosen band and class, it tells you what to fix and exits without firing anything. In this default mode it calls the dispatcher directly, so deduplication is *not* exercised and no `notified_alerts` row is written. Passing `--include-all-channels` opts into the full `on_new_scores` path, which adds the dedup pre-check and claim and delivers to every routed channel, and **will** email real recipients; use it only when that is what you want.

**Tier 3: live scoring.** Let the running engine score a genuine Critical or High transaction and emit it. This is the only tier that proves the deployed system alerts end to end, and it is the one you cannot schedule. For a standing version of it on a preprod host, `backend/scripts/webhook_testing/deploy/tms-webhook-test.service` runs the receiver as a systemd unit on loopback and `backend/scripts/webhook_testing/deploy/nginx-webhook-test.conf` routes an unguessable path on the existing TLS server to it, so alerts can be watched arriving with `journalctl -u tms-webhook-test -f`. The path token is obscurity, not authentication, and the sink prints and 200s whatever it receives: point it at preprod data only, never at a production alert stream.

**On signing.** Signing is off unless `WEBHOOK_SIGNING_SECRET` is set; the sender simply omits the header and delivery is otherwise identical. Set the secret on both sides or neither. A receiver holding a secret will flag every request from an unsigned sender.

Automated coverage for this deliverable is 97 tests across `backend/tests/notifications/` (92) and `backend/tests/api/test_notifications_config.py` (5), plus the frontend linter and settings-page tests; see [REPOSITORY-MAP.md](REPOSITORY-MAP.md#alerting) and [TESTING.md](TESTING.md).

## Common Misconfigurations

### An Enabled, Routed Channel With an Empty Recipient List

This is the one that happened in production, and it is first because it is the quietest.

A channel can be enabled, selected in the band defaults, and resolve to no recipients. Validation accepts this: an empty list is a legal list. Routing then drops the channel at `resolve_dispatch` with a WARNING per dropped alert, and because nothing was attempted, **no audit row is written**. Delivery stops completely while the configuration page still reads "Critical goes to email". The comment recording the incident in `frontend/src/lib/notification-warnings.ts` puts it at 121 dropped alerts over 11 days on mainnet before it was noticed.

Three ways it arises, and the third is the least obvious:

1. Clearing the global `recipients` on a channel that is routed in the band defaults.
2. Adding a per-rule `recipients` override that is present but empty. Because a rule replaces rather than extends (precedence rule 4), this suppresses the channel for that rule **even when the global list is populated**.
3. Leaving a `group:<alias>` entry in place after emptying the group, or after renaming it. `resolve_recipients` expands the alias to its members, so a channel whose only recipient is an alias resolving to nothing delivers to nobody. The list looks populated everywhere it is displayed.

The dashboard's pre-save linter (`configWarnings`) catches all three, counting recipients after group expansion so case 3 cannot hide behind a non-empty-looking list, and distinguishing an absent per-rule key (falls back to the global) from a present-but-empty one (replaces it). It also catches the webhook-with-no-URL equivalents, the inverse mistake of a channel that is enabled but routed nowhere, and any channel routed while switched off.

Warnings render at the top of the settings page under the heading "These settings won't deliver as-is". They are advisory: the linter warns, it does not block the save, because the backend deliberately accepts these documents. If you edit the configuration through the API rather than the dashboard you get no linting at all, so check `resolve_dispatch`'s behaviour with `engine_emit_test.py` afterwards.

To detect it after the fact:

```bash
docker compose logs app | grep "no resolved recipients or URL"
```

### The Environment Master Switches the UI Cannot See

Each channel is gated twice, and the dashboard only shows one of the two gates. The document's `enabled` flag is visible and editable; the environment switch is not.

| Channel | Also requires |
|---|---|
| email | `EMAIL_NOTIFY_ENABLED=true`, `SMTP_ENABLED=true`, and a non-empty `SMTP_HOST` |
| webhook | `WEBHOOK_NOTIFY_ENABLED=true` |

If any of those is off, the channel is dropped inside the dispatcher before the logging block, so there is **no log line and no audit row**. The dashboard will show the channel enabled and routed, and nothing will arrive. The one hint available in the UI is the "SMTP configured" badge on the email card, driven by the `secrets_status` block, which covers the `SMTP_HOST` and `SMTP_ENABLED` half of the condition but not `EMAIL_NOTIFY_ENABLED`.

When email is silently not sending, check the environment on the host before touching the configuration document.

### Environment Changes Need a Restart; Document Changes Do Not

The asymmetry catches people out. Pydantic settings are read once at process start, so every `NOTIFY_*`, `WEBHOOK_*`, `EMAIL_NOTIFY_*` and `SMTP_*` change requires restarting the application. The configuration document is refreshed in-process on every successful `PUT`, so channel toggles, recipient lists, the trigger matrix and the report settings all take effect on the next alert with no restart.

### A PUT That Omits periodic_report Turns the Report Off

The `PUT` handler drops fields whose value is `None`, and `periodic_report` defaults to `None` on the request model. A hand-written `PUT` body that omits the block therefore stores a document with no report configuration, the runtime defaults apply, and `enabled` reverts to `false`. Always round-trip the whole document: `GET`, modify, `PUT`.

For the same reason, a typo in a *nested* key is not caught. The validator rejects unknown bands, unknown attack classes, unknown channel names and undefined group aliases, but a misspelled optional field such as `recipeints` inside a channel block is simply stored and ignored, and the channel behaves as though it had no recipients.

### Routing a Class With a Rule and Losing the Band Default

Covered under [Routing and Precedence](#routing-and-precedence), but it belongs in this list too. Adding a rule to send one attack class somewhere extra silently removes every channel the band default was providing, because a matching rule replaces rather than merges. If a rule is meant to *add* a destination, its `channels` list must repeat the band default's channels as well.

### A Webhook URL That Is Refused or Blocked

Configuration-time validation refuses an internal-looking URL outright with a 422. At send time a fresh DNS lookup runs, and a host that resolves to a loopback, private, link-local or reserved address is blocked with a WARNING and an `ok=False` result, which does count as a failure and does produce an audit row. Both are bypassed by `WEBHOOK_ALLOW_INTERNAL=true`, which should be set only for a genuinely internal receiver such as an in-VPC SIEM.

Separately, if the webhook channel is enabled with a URL pointing at a public inspector (`webhook.site`, `requestbin`, `pipedream.net`, `beeceptor.com`, `hookbin.com`), a loud warning is logged at startup and after every edit. Full alert payloads including transaction hashes and scores would egress in plaintext to a third party. Those services are for preprod smoke tests only.

## Limitations

Stated plainly, because a monitoring system's alerting path should not be oversold.

**Configuration hot-reload assumes a single worker.** The cache is refreshed in the process that handles the admin `PUT`. With multiple uvicorn workers, or with an admin request load-balanced to a standby instance, the leader keeps alerting with its previous configuration until it restarts or handles an edit itself. Make notification-configuration edits against the leader, or restart the leader afterwards. Closing this properly needs a Postgres `LISTEN`/`NOTIFY`-driven refresh, which is out of scope for the current single-worker deployment.

**There is no pager or escalation channel.** Two channels exist: email and webhook. There is no SMS, no PagerDuty or Opsgenie integration, no on-call rotation, and no escalation if nobody acknowledges an alert. The webhook is the intended integration point for all of those; the payload is designed to be forwarded. [`backend/app/notifications/ADDING_A_CHANNEL.md`](../backend/app/notifications/ADDING_A_CHANNEL.md) documents adding a native channel, which touches only the channel file, one line in the registry, and the settings block.

**There is no dead-letter queue.** A failed delivery is logged and, for the scorer path, gone. Nothing is persisted for later replay, there is no outbox table, and there is no operator command to resend a specific alert.

**There is no per-recipient rate limit.** `NOTIFY_MAX_CONCURRENT_DELIVERIES` paces concurrent sends so a burst cannot open hundreds of simultaneous SMTP or webhook connections, and `NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK` bounds the poller's backlog drain. Neither is a rate limit: every routed alert is eventually attempted, just not all at once. A miscalibrated detector or a genuine attack wave will send as many alerts as it produces. This is a deliberate recall-first trade, and it means the receiver is responsible for its own throttling.

**The webhook signature has no replay window.** `X-TMS-Signature` is an HMAC-SHA256 of the raw body and nothing else. There is no timestamp in the signed material and no nonce, so a receiver cannot distinguish a fresh delivery from a captured one replayed later. A receiver that needs replay protection should deduplicate on `tx_hash` plus `timestamp` from the payload body.

**Configuration edits are audited but not diffed.** The audit row records who changed the notification configuration, from which IP, and when; its details payload carries the actor and nothing else. The previous document is not retained, so there is no way to see what a given edit changed or to roll it back from the audit trail.
