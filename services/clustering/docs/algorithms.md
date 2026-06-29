# Algorithms: Clustering & Anomaly Detection

This document describes exactly how transactions are turned into feature vectors,
how clusters are formed, how parameters are chosen, and how anomalies are scored.
All of it operates per **target** (one script address or minting policy id).

- Feature engineering: [features/](../backend/app/features/)
- Clustering + parameter evaluation: [clustering/](../backend/app/clustering/)
- Anomaly detection: [anomaly/detect.py](../backend/app/anomaly/detect.py)

A uniform `ClusteringInput` carries everything downstream needs:

```python
ClusteringInput(tx_hashes, data, metric, feature_set, feature_names)
#   data   : feature matrix (metric="euclidean") OR distance matrix (metric="precomputed")
#   metric : "euclidean" | "precomputed"
```

## 1. Feature engineering

Three feature sets, selected by name. Each answers a different question.

| Set | Metric | Groups transactions by | Builder |
|---|---|---|---|
| `shape` | euclidean | per‑tx value & structure | [shape.py](../backend/app/features/shape.py) |
| `graph` | precomputed (Jaccard) | shared addresses (entity / co‑spend) | [graph.py](../backend/app/features/graph.py) |
| `combined` | euclidean | shape + a graph embedding | [graph.py](../backend/app/features/graph.py) |

### 1.1 Shape features (`build_shape_features`)

Per‑transaction numeric features. Monetary/count columns are heavy‑tailed, so each
is **signed‑log transformed** then scaled with a **RobustScaler** (median / IQR) so
whales don't dominate the Euclidean geometry.

Log‑scaled columns (9): `fees`, `size`, `input_count`, `output_count`,
`total_input_lovelace`, `total_output_lovelace`, `net_lovelace`
(= out − in), `distinct_assets`, `redeemer_count`.

> **`net_lovelace` semantics.** `out − in ≈ −(fee + deposit) + withdrawals`, so
> for ordinary transactions it is nearly collinear with `fees`; its independent
> signal is deposits/refunds and reward withdrawals. It is *not* an economic
> P&L for the contract (that would need fee/deposit decomposition and
> target‑scoped flows). A `fee_per_byte` replacement is a candidate
> feature‑set change — deferred because any feature change invalidates every
> stored model (forces re‑fits across all contracts).

```
signed_log1p(x) = sign(x) · log1p(|x|)      # handles negative net_lovelace
```

Time is **cyclically encoded** so it wraps continuously:

- `hour_of_day` (0–23) → `(sin, cos)` with period 24
- `day_of_week` (ClickHouse 1=Mon…7=Sun, shifted to 0–6) → `(sin, cos)` with period 7

Result: **13 features** (9 scaled magnitudes + 4 cyclical). RobustScaler maps a
zero‑IQR column to scale 1 (no division blow‑up). Distance is Euclidean.

### 1.2 Graph features (`build_jaccard_distance`)

Each transaction is the **set of entities** in its UTXOs. Two transactions are
"close" if their entity sets overlap — capturing entity structure / co‑spend
behavior that shape features can't see.

**Entity resolution (Cardano‑specific).** A Cardano wallet is one *stake
credential* controlling many payment addresses, and dApps rotate change/one‑time
addresses constantly — so keying on the raw payment address makes one wallet look
like many distinct entities. `entity_key` ([graph.py](../backend/app/features/graph.py))
therefore resolves each address to its **stake credential** when derivable (base
and reward addresses, decoded offline via [bech32.py](../backend/app/registry/bech32.py)
— no extra API calls) and falls back to the raw address for enterprise / pointer /
Byron / script addresses that have no stake part. This collapses a wallet's many
addresses into a single node, materially improving co‑spend detection.

- Build a sparse binary **tx‑by‑address incidence matrix** `M`.
- Intersection counts: `inter = M · Mᵀ`. Set sizes: `|A|, |B|`.
- Jaccard **similarity** `= inter / (|A| + |B| − inter)`; **distance** `= 1 − sim`,
  diagonal forced to 0, clipped to `[0, 1]`.

This distance matrix is **dense, O(n²)** in memory and time. When a target exceeds
`MAX_GRAPH_TXS` (default 5000) the tx set is **deterministically down‑sampled** (and
the drop is logged) to bound the `n×n` allocation. DBSCAN runs on it with
`metric="precomputed"`.

> ⚠️ Raising `MAX_GRAPH_TXS` raises memory ~quadratically (`5000² × 8 bytes × few
> arrays ≈ hundreds of MB`). For full coverage on large targets prefer `combined`.

### 1.3 Combined features (`build_combined_features`)

Shape features concatenated with a low‑dimensional **TruncatedSVD embedding**
(`k = min(8, n_addr−1, n_tx−1)` components) of the same tx‑by‑address incidence,
StandardScaler‑normalized. Gives a dense Euclidean matrix that blends value/shape
with graph structure and scales better than the dense Jaccard matrix.

## 2. Clustering with DBSCAN

DBSCAN ([dbscan.py](../backend/app/clustering/dbscan.py)) groups dense regions and
labels the rest **noise** (`cluster_id = -1`). It needs no preset cluster count and
its noise label is exactly what we want for "unusual" transactions.

Two parameters:

- **`eps`** — neighbourhood radius (in the feature/distance space).
- **`min_samples`** — points required to form a dense core.

`metric` is `euclidean` for shape/combined and `precomputed` for graph (the matrix
*is* the distances). Each run records `n_clusters`, `n_noise`, and the **silhouette**
score.

### Silhouette

Computed over non‑noise points only (cohesion vs separation, range `[-1, 1]`,
higher = better). Undefined (stored as `NaN`, surfaced as `null`) when there are
fewer than 2 clusters or fewer than 2 non‑noise points. For the precomputed metric
it is computed directly on the distance sub‑matrix.

## 3. Choosing the parameters (`evaluate`)

The "decide the parameters" step ([evaluate.py](../backend/app/clustering/evaluate.py))
is what makes results defensible rather than arbitrary. It combines a classic
heuristic with a small grid search. (Needs ≥ 3 points; below that it returns a
"not enough transactions" message and no recommendation.)

### 3.1 k‑distance knee → suggested `eps`

Sort every point's distance to its **k‑th nearest neighbour** (`k = min_samples`).
The "knee" of that ascending curve is the classic `eps` heuristic — below it points
are dense, above it they're sparse.

- Detected with `KneeLocator` (convex, increasing).
- **Fallback:** if no knee is found, the 90th percentile of the curve.
- The curve is down‑sampled to ≤ 1500 points for plotting in the UI.

### 3.2 `min_samples` heuristic (`default_min_samples`)

Shared by the grid and the anomaly detector so they agree:

- precomputed (graph): **4**
- otherwise: `2 × n_features`, clamped to `[4, 24]`.

### 3.3 Grid search

Score an `eps × min_samples` grid by `(silhouette, cluster count, noise ratio)`:

- **eps grid:** `knee × {0.5, 0.75, 1.0, 1.25, 1.5, 2.0}` for Euclidean; fixed
  `{0.2, 0.3, 0.4, 0.5, 0.6, 0.7}` for the bounded Jaccard metric.
- **min_samples grid:** `sorted({4, base, min(2·base, 32)})`, filtered to valid range.

### 3.4 Recommendation

> **Highest silhouette** among configs with **≥ 2 clusters** and **< 90 % noise**.
> Zero noise is allowed (often ideal). If no config qualifies, fall back to
> `(knee_eps, base_min_samples)`.

The per‑contract pipeline feeds this recommendation straight into the shape cluster
run; the UI also exposes the k‑distance chart and the scored grid so a human can
override.

## 4. Anomaly detection (ensemble)

`detect_anomalies` ([detect.py](../backend/app/anomaly/detect.py)) ranks every
transaction by an **ensemble of three complementary unsupervised detectors** and
fuses them. Each captures a different notion of "unusual":

| Detector | Catches | Notes |
|---|---|---|
| **Isolation Forest** | global rarity in feature space | 300 trees, `contamination="auto"`. **Skipped** for the precomputed metric (needs feature vectors) → its score is `NaN`. |
| **Local Outlier Factor (LOF)** | local density deviation | `n_neighbors = min(20, n−1)`; uses `precomputed` or `minkowski` to match the metric. |
| **DBSCAN noise** | points outside every dense region | reuses the same DBSCAN; `eps`/`min_samples` auto‑derived (knee + heuristic) unless supplied. |

### Fusion

1. **Rank‑normalize** each detector's scores to `[0, 1]` (rank‑percentile —
   robust to scale and outliers).
2. **Consensus** = mean of the available normalized signals (`[0, 1]`).
3. **Votes** (0–3) = how many detectors independently flag the point: each
   detector flags its top `top_quantile` (default **5 %**); DBSCAN flags its noise.
4. **Rank** = order by descending consensus (1 = most anomalous).
5. **`n_flagged`** = transactions with **≥ 2 votes** — a far stronger signal than
   any single detector's top pick, and what the UI highlights.

For the precomputed (graph) metric, only LOF + DBSCAN contribute (Isolation Forest
is skipped), so votes range 0–2 there.

> These surface **statistically anomalous** transactions for human review — not
> provably malicious ones. There is no ground‑truth label.

### Persisted per transaction

`iso_score`, `lof_score`, `dbscan_noise` (0/1), `consensus`, `votes`, `score_rank`
— see [data-model.md](data-model.md) (`anomaly_runs` / `anomaly_scores`).

### Batch vs online voting semantics (by design, do NOT "align")

The batch ensemble and the online (frozen-model) scorer answer **different
questions** with deliberately different thresholds:

| | Batch (`detect_anomalies`) | Online (`score_shape`) |
|---|---|---|
| Per-detector flag | top `top_quantile` **by rank** within *this run's population* | raw score ≥ a **frozen value threshold** (the training set's `1−top_quantile` quantile) |
| What counts as a vote | iso + lof + **DBSCAN-noise** (3, computed over the live population — genuinely independent) | iso + lof **only** (0–2). The "online-noise" flag (`cluster_id = -1`) is **not** a vote — it is collinear with iso/lof novelty, so counting it would triple-count one "far from training" fact |
| Verdict rule | `votes ≥ 2` of 3 | **all available detectors agree** (`votes == n_detectors`; both are always fit together, so this is `votes ≥ 2` of 2) |
| Question answered | "is this tx among the most unusual *of this run*?" | "would this tx have been training-top-5% on *both* independent detectors?" |
| Population effect | always flags ~5% of any population | can flag 0% or 100% of an incoming batch |

Rank-normalizing the online batch instead would be wrong: a batch of three
perfectly normal incoming txs would always flag its relative-max. The frozen
value threshold is the correct online semantics — but it means online `votes`
are not numerically comparable to a batch re-run on the same data, and online
`consensus` is normalized against *training* score bounds (clips at 1.0 for
points more extreme than anything in training) rather than rank-normalized.
The online-noise flag is excluded from `votes` precisely because, unlike batch
DBSCAN-noise, it cannot be independent: the online path can't form new clusters,
so a drifted-but-benign tx lands as noise *and* scores high on iso/lof — requiring
the two genuinely independent detectors to agree is what keeps the false-positive
rate near the intended ~5%. `consensus` still folds in the noise signal so
unassigned points rank higher for human review.

**The noise rate is the drift signal.** When the fraction of recently classified
txs that land unassigned (`cluster_id = -1`) rises, incoming traffic no longer
fits the frozen model — it's stale. `update_contract` records this as
`contracts.drift_score` and the API derives `reclustering_suggested`
(`≥ RECLUSTER_NOISE_THRESHOLD`, default 0.25); the UI surfaces a "re-cluster
recommended" badge. Re-cluster is the population-relative remedy (the batch path
re-forms clusters on current data); it is never automatic. See
[online-classification-design.md](online-classification-design.md).

## 5. How the per-contract pipeline uses all of this

For each watched contract the scheduler onboards as the chain is ingested,
`process_contract` runs (on a single shared shape matrix, plus one graph matrix):

1. **Shape cluster** — `evaluate(shape)` → recommended `(eps, min_samples)` →
   DBSCAN. Persisted as a `cluster_run` + per‑tx `cluster_labels`.
2. **Shape anomaly** — ensemble on the shape matrix (auto `eps`/`min_samples`).
3. **Graph anomaly** — ensemble on the Jaccard distance matrix (LOF + DBSCAN only).

So every contract gets one shape clustering and two anomaly runs (shape + graph),
all comparable across contracts. The module's API also allows ad‑hoc `evaluate`,
`cluster`, and `anomaly` runs with any feature set and parameters.

## 6. Assumptions, limitations & confidence

Read this before trusting an output. These methods surface **statistically
unusual** transactions and **co‑spend structure** for human review — there is no
ground truth and nothing here is a proof of intent.

| Area | Assumption / limitation | Confidence |
|---|---|---|
| **Entity resolution** | Co‑spend uses the **stake credential** where derivable, else the raw address. Enterprise/pointer/Byron/script addresses can't be grouped by wallet, so a contract used mostly via enterprise addresses still under‑groups. | Medium‑high |
| **Collateral / invalid txs** | The collateral‑return output of a script‑failed tx is **excluded** from output features (it would otherwise inflate output volume on exactly the anomalous txs). The collateral flag on each output is carried through from the TMS's Ogmios‑ingested chain data in `tms_analytics`, so the exclusion is reliable. | High |
| **DBSCAN density** | A single global `eps` assumes roughly uniform density. Transaction populations are multi‑scale (whale clusters + a long tail); very mixed‑density targets may be better served by HDBSCAN (a candidate upgrade). | Medium |
| **Graph scale** | The Jaccard matrix is dense O(n²); above `MAX_GRAPH_TXS` (5000) the tx set is **sampled deterministically by hash** (not a behavioral sample). Use `combined` for full coverage on large targets. | Medium |
| **Anomaly votes asymmetry** | Shape anomaly has 3 detectors, graph only 2 (no Isolation Forest), yet both flag at `votes ≥ 2` — so a graph flag requires *both* detectors to agree, a stricter bar. Graph `n_flagged` is therefore systematically lower. | Documented |
| **Time features** | `block_time` drives the cyclical hour/day features; confirmed txs always carry it. Mempool/unconfirmed txs are not ingested. | High |
| **Policy mint coverage** | Verified empirically (2026‑06, SpaceBudz policy): an asset's transaction history in `tms_analytics` **includes the mint tx** (it is the asset's first entry), so policy‑target discovery covers minting: a previously suspected gap that does **not** exist. Burns involve the asset's inputs and are likewise listed. | High |
| **Failed‑tx inputs** | A script‑failed tx consumes **only its collateral inputs**; features/co‑spend model that consumed set (regular inputs on failed txs are phantoms and are excluded), so the collateral spender — who authorized the failed attempt — is visible. | High |
| **No supervised validation** | Unsupervised throughout; no precision/recall. The manual `tx_labels` verdicts are the intended seed for building an evaluation set over time. | n/a |

**Reproducibility:** Isolation Forest and TruncatedSVD use `random_state=0`;
RobustScaler/LOF/DBSCAN are deterministic given their inputs. Same data + same
params ⇒ same result.
