/**
 * Watched Validators: manage the contracts the clustering sidecar monitors.
 *
 * The page is the shell: it gates on the clustering module being enabled, loads
 * the watchlist + live jobs, and renders the add-form and a grid of contract
 * cards. The form (`AddContractForm`) and each card (`ContractCard`) own their
 * own behaviour. New transactions are classified automatically as the chain is
 * ingested; the card actions are conveniences on top of that auto feed.
 */
import { useMemo } from "react";

import { AddContractForm } from "@/components/clustering/AddContractForm";
import { ContractCard } from "@/components/clustering/ContractCard";
import { latestJobForTarget } from "@/components/clustering/jobStage";
import { useContracts, useJobs } from "@/lib/api/clustering";
import type { Contract } from "@/lib/api/clustering";
import { useHealth } from "@/lib/api/health";

/**
 * Stable display order for the card grid. The watchlist API orders by
 * `updated_at DESC`, which churns on every poll (job progress, drift, status,
 * tx_count all bump `updated_at`), so cards jumped positions every ~10s and
 * were hard to click. Order by fields that change only on explicit user
 * action: labelled validators first (alphabetical), then unlabelled ones by
 * their immutable address. Nothing here moves on a background refresh.
 */
function byLabelThenTarget(a: Contract, b: Contract): number {
	const la = a.label?.trim() ?? "";
	const lb = b.label?.trim() ?? "";
	if (Boolean(la) !== Boolean(lb)) return la ? -1 : 1; // labelled first
	const byLabel = la.localeCompare(lb);
	if (byLabel !== 0) return byLabel;
	return a.target.localeCompare(b.target);
}

export function ValidatorsPage() {
	const health = useHealth();
	// Hold the clustering polls until health confirms the module is on, so a
	// disabled deployment (or the brief pre-health window) never hits
	// /api/clustering/*. `undefined` keeps each hook's default poll interval.
	const clusteringEnabled = health.data?.clustering_enabled === true;
	const {
		data: contracts,
		isLoading,
		isError,
	} = useContracts(undefined, clusteringEnabled);
	const { data: jobs } = useJobs(undefined, clusteringEnabled);

	// Sort a copy so the rendered order stays stable across polls regardless of
	// the order the API returns rows in.
	const sortedContracts = useMemo(
		() => (contracts ? [...contracts].sort(byLabelThenTarget) : contracts),
		[contracts],
	);

	if (health.data && health.data.clustering_enabled === false) {
		return (
			<div className="text-muted-foreground text-sm">
				The clustering module is not enabled on this deployment.
			</div>
		);
	}

	return (
		<div className="space-y-6">
			<div>
				<h1 className="text-xl font-semibold">Watched Validators</h1>
				<p className="text-muted-foreground text-sm">
					Contracts monitored by the clustering engine. New transactions are
					classified automatically as the chain is ingested; anomalies surface
					as the <span className="font-medium">Contract Anomaly</span> attack
					type.
				</p>
			</div>

			<AddContractForm />

			{isLoading ? (
				<p className="text-muted-foreground text-sm">Loading watchlist…</p>
			) : isError ? (
				<p className="text-destructive text-sm">
					Could not load the watchlist. The clustering service may be
					unavailable; retry shortly.
				</p>
			) : !sortedContracts?.length ? (
				<p className="text-muted-foreground text-sm">
					No contracts watched yet. Add a script address above to start.
				</p>
			) : (
				<div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
					{sortedContracts.map((c) => (
						<ContractCard
							key={c.target}
							c={c}
							job={latestJobForTarget(jobs, c.target)}
						/>
					))}
				</div>
			)}
		</div>
	);
}
