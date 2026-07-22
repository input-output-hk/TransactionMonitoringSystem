# Clustering and anomaly detection

This guide explains the two unsupervised analyses the Validators surface offers:
**clustering** and **anomaly detection**. Read it before relying on their output.
Both are exploratory tools that surface transactions worth a closer look; neither
is a proof of intent, and neither changes the canonical risk scoring.

## What these tools do

Each watched validator (a script address or minting policy) is analysed two ways
over the same set of transactions:

- **Clustering** groups transactions that resemble each other, so you can see the
  validator's normal behaviour as a few dense groups plus a scattering of
  outliers.
- **Anomaly detection** scores every transaction by how unusual it is and ranks
  the most extreme ones for review.

The system runs both automatically for every validator and keeps the result as the
canonical **System** run. The controls in the UI let you run your own **Custom**
passes for experimentation; those never replace the System run (see
[Custom runs vs the System run](#custom-runs-vs-the-canonical-system-run)).

## Clustering

Clustering uses **DBSCAN**, which finds dense regions and labels everything else
**noise**. It needs no preset number of clusters, and its noise label is exactly
what "unusual transaction" means here. It has two parameters:

- **eps**: the neighbourhood radius in the feature space.
- **min_samples**: how many points must sit within `eps` to form a dense core.

Each run reports the number of clusters, the noise count, and a **silhouette**
score (a cohesion-versus-separation measure from -1 to 1, higher is better; shown
as "-" when it cannot be computed, for example with fewer than two clusters).

## Anomaly detection

Anomaly detection ranks every transaction with an **ensemble of three
complementary detectors**, each capturing a different notion of "unusual":

- **Isolation Forest**: global rarity, the kind of value combination a random
  decision tree can isolate in just a few splits. Skipped on a graph run (it needs
  feature vectors), so it shows "-" there.
- **Local Outlier Factor (LOF)**: local density, a transaction sitting in a much
  sparser neighbourhood than its nearest peers.
- **DBSCAN noise**: transactions that fall outside every dense cluster.

The detectors' scores are rank-normalised to a 0 to 1 range and averaged into a
**consensus** score, which sets the rank. Separately, each detector flags its most
extreme roughly 5%; the **votes** column counts how many detectors flagged a given
transaction. Two or more votes is the strong signal, and it is what the table
highlights. On a graph run only LOF and DBSCAN contribute, so votes range from 0 to
2 there and a flag requires both detectors to agree.

These surface **statistically unusual** transactions for human review, not provably
malicious ones. There is no ground-truth label.

## Feature sets: shape, graph, combined

Both tools can compare transactions three ways, selected with the **Feature set**
control:

- **shape**: each transaction as a vector of per-transaction numbers (fees, size,
  input/output counts, ADA in/out and net, distinct assets, redeemer count, and a
  cyclically encoded time-of-day). Groups transactions that look alike regardless
  of who is involved. Euclidean distance; fast; the default.
- **graph**: compares the set of addresses each transaction touches (Jaccard
  distance), resolving addresses to their stake credential where possible so one
  wallet's many addresses collapse to a single entity. Finds shared-counterparty
  and co-spend structure that shape cannot see. This is dense and grows with the
  square of the transaction count, so it is capped (large validators are
  down-sampled); prefer combined for full coverage at scale.
- **combined**: shape features plus a compact embedding of the address graph. Both
  signals at once, and it scales better than graph on large validators.

## Reading the results: votes and verdicts

In the Anomalies table:

- **Consensus**: the averaged, rank-normalised score (0 to 1); higher is more
  anomalous. It sets the rank.
- **Votes**: how many detectors independently flagged the transaction (0 to 3 on a
  shape run, 0 to 2 on a graph run). Two or more is the strong signal.
- **Iso / Lof / Dbscan**: the individual detector signals behind the consensus.
- **Verdict**: the effective call. An analyst's manual label (malicious or benign)
  always wins; otherwise it reads "anomaly" when at least two detectors agree, else
  "normal". A high-scoring transaction can therefore read benign (a reviewed false
  positive) and a low-scoring one malicious: the human has the last word.

Scores rank transactions **within a run**; they are not absolute thresholds, and
statistically unusual does not mean malicious.

## Escalating a finding: from review to the Attacks page

Clustering and anomaly detection are for review; on their own they never raise a
canonical alert. The way you turn a reviewed finding into an alert on the main
Attacks page is to **label it**:

- Labeling a transaction or a cluster **malicious** publishes those transactions to
  the Attacks page as the `contract_anomaly` attack class, and can fire a
  notification. This is the intended path for promoting a finding: a human confirms
  it, then it becomes a canonical alert.
- Labeling **benign** does the opposite: it clears the transaction, suppressing any
  anomaly signal so it does not surface.

A label is a per-transaction human judgement stored against the transaction itself,
not against a run. So it works the same whether you were viewing the System run or
one of your own Custom runs: if you spot a real attack while exploring a custom pass,
labeling it malicious still escalates it.

There are two ways to flag, differing only in scope:

- A **cluster label** applies your verdict to every current member of the cluster,
  and it **propagates**: transactions that later cluster alongside a labeled one
  inherit the verdict. Use it when the whole group shares a judgement. The noise
  bucket cannot be cluster-labeled (its points share no pattern).
- A **single-transaction label** colours only that one transaction and does **not**
  propagate. Use it for an individual judgement, for example a noise-bucket outlier
  that belongs to no cluster.

Malicious labels reach the Attacks page regardless of which run surfaced the
transaction; only the automatic verdicts are run-scoped (see the next section).

## Custom runs vs the canonical System run

Every validator has a **System** run: the automatically tuned run that drives
scoring and verdicts. When you use "Re-cluster with custom parameters" or "Re-run
anomaly scoring" you create a separate **Custom** run:

- A custom run is an experiment. It is kept separate, badged Custom, and can be
  deleted freely.
- A custom run's **automatic** output never feeds scoring or the Attacks page. The
  online model always fits from the System run only, and only the System run's
  auto-verdicts are published as `contract_anomaly` alerts. To send a finding from a
  custom run to the Attacks page, label it malicious (see [Escalating a
  finding](#escalating-a-finding-from-review-to-the-attacks-page)): that is an
  explicit human judgement, not the run's automatic output.

Because custom runs are safe and disposable, use them freely to explore alternative
parameters or feature sets; just remember their automatic results are yours to
interpret, not a change to the validator's canonical assessment.

## Choosing DBSCAN parameters

The "Advanced: tune parameters" panel helps you pick `eps` and `min_samples`
defensibly rather than by guesswork:

- A **k-distance curve** plots each transaction's distance to its k-th nearest
  neighbour, sorted ascending. Its "knee" is the classic starting point for `eps`:
  below it points are dense, above it they are sparse.
- A small **grid search** scores several `(eps, min_samples)` pairs around the knee
  by silhouette, cluster count, and noise ratio, and recommends the best qualifying
  configuration (highest silhouette with at least two clusters and under 90% noise).

Click any grid row to load its parameters into the controls, then run the custom
pass. The System run already uses this recommendation; the panel is for trying
alternatives.

## When re-clustering is suggested

As new transactions arrive, the share that no longer fits the frozen model (the
proportion landing as noise) is tracked as a drift signal. When it rises past the
configured threshold, the UI surfaces a "re-cluster recommended" hint. Re-clustering
re-forms the groups on current data; it is never automatic, and it remains your
decision.

## Limitations: read before trusting an output

These methods surface statistically unusual transactions and co-spend structure for
human review. There is no ground truth and nothing here proves intent. Key caveats:

- **Entity resolution**: co-spend grouping uses the stake credential where it can be
  derived, otherwise the raw address. Enterprise, pointer, Byron, and script
  addresses cannot be grouped by wallet, so a validator used mostly through such
  addresses will under-group.
- **Graph scale**: the graph feature set is memory-heavy and is down-sampled above
  its cap; use combined for full coverage on large validators.
- **Vote asymmetry**: shape runs have three detectors, graph runs only two, yet both
  flag at two or more votes; a graph flag therefore needs both detectors to agree, a
  stricter bar, so graph flags are systematically fewer.
- **No supervised validation**: the pipeline is unsupervised end to end; there is no
  precision or recall figure. Manual verdict labels are the intended seed for
  building an evaluation set over time.

Reproducibility: given the same data and the same parameters, a run produces the
same result.
