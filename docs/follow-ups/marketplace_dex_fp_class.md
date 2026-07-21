# Follow-up: Marketplace / DEX Transaction False-Positive Class

## Status: identified on mainnet (2026-07-21), not yet mitigated

The 2026-07-21 mainnet triage (multiple_sat saturation-floor rollout plus the
phishing / token_dust precision fixes) confirmed a residual false-positive
class that cuts across two scorers: transactions produced by NFT marketplaces
and DEX aggregators. These are legitimate protocol operations whose on-chain
shape collides with two threat models.

### phishing

A URL-named token or ADA Handle that rides through a DEX aggregator swap or a
marketplace trade lands at a *different* address than the exact input holder
(the order UTxO, the router, the buyer). The net-new-holder targeting signal
therefore reads it as a delivery rather than self-change, so the two stacking
bonuses (`url_combo_bonus`, `phishing_tld_bonus`) are kept and the tx pages
High. This is the "entity clustering would fix it" limitation noted on the
self-change gate, now observed live. Confirmed cases:

| tx (prefix) | URL-shaped name | route |
|---|---|---|
| `19ae239a` | `$cardano.me` (ADA Handle, policy `f0ff48bb`) | Minswap aggregator swap |
| `3b56f1b8`, `a413fcb0`, `5a7332ab`, `6aa6e386` | `cardano.blue` (policy `74be1f69`) | Minswap routing / transfers |
| `4acfd980` | `jpg.store`, `ChainPort.io`, `numero.uno` (ADA Handles + official bridged COPI) | Wayup marketplace |

### token_dust

A marketplace bulk sale of a legitimate but *recent* NFT collection is below
`established_collection.min_policy_age_slots` (30 days), so the
established-collection cap does not apply and the many-assets-per-policy shape
pages High. Confirmed case: `fb2a443f`, a Wayup sale of 200 Cornucopias
`GenesisBronzeHoodie` NFTs from a policy first seen ~5 days earlier.

### Common thread

All of these carry a CIP-20 (label 674) `msg` naming the operator:
`"Wayup Transaction"`, `"Minswap: Routing Order"`,
`"Minswap: Aggregator Market Order"`, and similar. More reliably, they spend
or pay a small set of well-known marketplace / aggregator script addresses.

## Why deferred

Under the recall-first rule these are acceptable false positives (each was
dismissed on review and archived), and the priority for the 2026-07 work was
the recall gap plus the two targeted precision fixes. A cross-scorer
discriminator is a distinct change that must be built so it cannot silence a
real detection.

## Proposed approach

A shared marketplace / DEX discriminator, mirroring the existing
`token_dust.established_collection` cap:

1. Identify the transaction as marketplace / aggregator activity. The
   authoritative signal is involvement of a known operator script (spends or
   pays a `marketplace_operators` address, network-scoped config). The label
   674 `msg` is a secondary hint only: it is attacker-forgeable free text, so
   it must never be sufficient on its own.
2. When identified, CAP the affected class to the top of Moderate. Never
   suppress: a genuine phishing airdrop or dust attack that happens to route
   through a marketplace or DEX must stay recorded, reported, and
   corroboration-eligible. Capping (not suppressing) also contains the
   forged-`msg` evasion: the worst an attacker gains by faking the operator
   signal is a Moderate band, still fully visible.
3. Ship with attack-must-fire tests: a real URL-token airdrop and a real dust
   bundle, each wrapped in marketplace metadata, must still score in-band.
4. Keep the operator list config-driven, network-scoped, with a REVIEW-BY
   discipline (operators and their scripts change).

## Cost / risk summary

| Aspect | Cost | Notes |
|---|---|---|
| Recall | None by construction | Cap, never suppress; findings stay visible and corroboration-eligible. |
| Precision | Removes the dominant residual High-band FP class | 6 of 7 non-multiple_sat mainnet High FPs on 2026-07-21 were marketplace / DEX. |
| Evasion | Forged operator `msg` | Mitigated by keying on operator scripts, not text, and by capping rather than suppressing. |
| Maintenance | Operator script list drifts | Network-scoped config with REVIEW-BY dates, same discipline as the allowlists. |

## Definition of done

- [ ] `marketplace_operators` config block (network-scoped operator scripts) with the validated loader.
- [ ] Shared detection of operator involvement (script-based; 674 `msg` secondary).
- [ ] phishing and token_dust cap to Moderate on a positive match; never suppress.
- [ ] Attack-must-fire tests: URL-token airdrop and dust bundle wrapped in marketplace metadata still score in-band.
- [ ] Backtest against the 2026-07-21 archived FPs: all drop out of High, the archived front-runner and any real signals are unaffected.
