/**
 * Collapsible terminology for the Anomalies surface: how the detector ensemble
 * works, and what each score column means. Co-located with the anomaly
 * components and kept out of the detail page so the page stays a thin router.
 */
import { HelpDetails } from "@/components/ui/help-details";

export function AnomalyHelp({ showColumnKey }: { showColumnKey: boolean }) {
	return (
		<div className="space-y-2">
			<HelpDetails summary="How anomalies are detected">
				<p>
					Every transaction is scored by an ensemble of three unsupervised
					detectors (two on a graph run) and ranked. Each detector independently
					flags its most extreme ~5%; transactions flagged by two or more (the{" "}
					<strong>votes</strong> column) are the strongest candidates.
				</p>
				<ul>
					<li>
						<strong>Isolation Forest (Iso):</strong> flags rare combinations of
						values, the kind a random decision tree can isolate from the rest in
						just a few splits.
					</li>
					<li>
						<strong>Local Outlier Factor (Lof):</strong> flags transactions
						sitting in a much sparser neighbourhood than their nearest peers
						(local density).
					</li>
					<li>
						<strong>DBSCAN noise:</strong> flags transactions that fall outside
						every dense cluster.
					</li>
				</ul>
				<p>
					The detectors compare a transaction's <strong>shape</strong>: fees,
					size, input/output counts, ADA in/out and net, distinct assets,
					redeemer count, and time-of-day. A <strong>graph</strong> run instead
					compares the set of addresses each transaction touches.
				</p>
				<p>
					These are statistically unusual transactions worth a closer look, not
					proof of anything.
				</p>
			</HelpDetails>

			{/* The column key only helps when the table below is actually
			    rendered, so gate it on there being a run to show. */}
			{showColumnKey && (
				<HelpDetails summary="What do the score columns mean?">
					<ul>
						<li>
							<strong>Consensus:</strong> the averaged, rank-normalised score
							across the detectors (0 to 1); higher = more anomalous. The bar
							visualises it, and it sets the rank (#).
						</li>
						<li>
							<strong>Votes:</strong> how many detectors independently flagged
							this tx (each flags its most extreme ~5%). 0 to 3 on a shape run,
							0 to 2 on a graph run; <strong>2 or more</strong> (highlighted
							row) is the strong signal.
						</li>
						<li>
							<strong>Iso:</strong> Isolation Forest: how rare this tx's
							combination of values is (rarer = higher). Shown as "—" on graph
							runs, which have no feature vectors.
						</li>
						<li>
							<strong>Lof:</strong> Local Outlier Factor: how much sparser this
							tx's neighbourhood is than its nearest peers (≈1 is normal, above
							1 = local outlier).
						</li>
						<li>
							<strong>Dbscan:</strong> ✓ when DBSCAN labelled this tx as noise:
							outside every dense cluster.
						</li>
						<li>
							<strong>Verdict:</strong> the effective call. The three scores
							above are the statistical evidence; this is the judgement. An
							analyst's manual label (malicious/benign, applied with the inline
							Label control) overrides the detectors; otherwise it is{" "}
							<em>anomaly</em> when ≥2 detectors agree, else <em>normal</em>. So
							a high-scoring tx can read benign (a reviewed false positive) and
							a low-scoring one malicious: the human has the last word, not a
							4th detector. Labeling a tx malicious also sends it to the main
							Attacks page as a contract_anomaly alert; this works from any run,
							including a custom one.
						</li>
					</ul>
					<p>
						Scores rank transactions within this run; they are not absolute
						thresholds, and statistically unusual does not mean malicious.
					</p>
				</HelpDetails>
			)}
		</div>
	);
}
