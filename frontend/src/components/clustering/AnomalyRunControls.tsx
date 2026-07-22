/**
 * Anomaly-run management bar. The run picker (view a saved run) and the glossary
 * stay visible because viewing is read-only and safe for everyone. Scoring a
 * custom run is an expert action, so it lives in a closed "Advanced" disclosure:
 * a non-technical Admin is not tempted to click it, and the copy there explains
 * that the normal way to refresh after drift is to Re-analyze the contract (which
 * regenerates the System run), not to score a custom run. The system run is the
 * canonical scored run and the default selection; a custom run never changes
 * scoring. Delete is Admin-only and custom-only (guarded server-side). Sibling of
 * `ClusterRunControls`.
 */
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { HelpDetails } from "@/components/ui/help-details";
import { Label } from "@/components/ui/label";
import {
	type AnomalyRun,
	type FeatureSet,
	isPermissionDenied,
	useDeleteAnomalyRun,
	useDetectAnomaly,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { DOCS_ANOMALY } from "@/lib/docs";
import { AdminOnlyGate } from "./adminOnly";
import { DocsCallout } from "./DocsCallout";
import { FeatureSetSelect } from "./FeatureSetSelect";
import { RunSelect } from "./RunSelect";

function runLabel(r: AnomalyRun): string {
	return `${r.feature_set} · ${r.n_flagged} flagged of ${r.n_points}`;
}

type Props = {
	target: string;
	runs: AnomalyRun[];
	selectedRunId: string;
	onSelectRun: (runId: string) => void;
};

export function AnomalyRunControls({
	target,
	runs,
	selectedRunId,
	onSelectRun,
}: Props) {
	const { isAdmin } = useAuth();
	const [featureSet, setFeatureSet] = useState<FeatureSet>("shape");
	const [confirmDelete, setConfirmDelete] = useState(false);
	const detect = useDetectAnomaly();
	const remove = useDeleteAnomalyRun();

	const selectedRun = runs.find((r) => r.run_id === selectedRunId) ?? null;
	// Deleting a run is Admin-only at the proxy; only offer it to an Admin.
	const canDelete = isAdmin && selectedRun?.origin === "custom";
	// The anomaly bar can mount before any run exists (its empty state routes the
	// user here to create the first one), so the action reads "Run" the first time
	// and "Re-run" afterwards.
	const scoreVerb =
		runs.length > 0 ? "Re-run anomaly scoring" : "Run anomaly scoring";

	const onDetect = () =>
		detect.mutate(
			{ target, feature_set: featureSet },
			{ onSuccess: (run) => run?.run_id && onSelectRun(run.run_id) },
		);

	const onDelete = () => {
		if (!selectedRun) return;
		remove.mutate(selectedRun.run_id, {
			onSuccess: () => {
				setConfirmDelete(false);
				// Fall back to the system run (or the first remaining).
				const fallback =
					runs.find(
						(r) => r.origin === "system" && r.run_id !== selectedRun.run_id,
					) ?? runs.find((r) => r.run_id !== selectedRun.run_id);
				onSelectRun(fallback?.run_id ?? "");
			},
		});
	};

	return (
		<div className="border-border space-y-3 rounded-md border p-4">
			{/* Run selection (view a saved run): read-only and safe for everyone, so
			    it stays at the top. Creating a run is an expert action in the
			    "Advanced" disclosure below. */}
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-muted-foreground text-sm">Run</span>
				<RunSelect
					runs={runs}
					value={selectedRunId}
					onChange={onSelectRun}
					getLabel={runLabel}
				/>
				{selectedRun && (
					<Badge
						variant={selectedRun.origin === "system" ? "outline" : "medium"}
					>
						{selectedRun.origin === "system" ? "System" : "Custom"}
					</Badge>
				)}
				{canDelete && (
					<Button
						variant="outline"
						size="sm"
						className="ml-auto"
						disabled={remove.isPending}
						onClick={() => setConfirmDelete(true)}
					>
						Delete run
					</Button>
				)}
			</div>

			{remove.isError && (
				<p className="text-destructive text-sm">
					{isPermissionDenied(remove.error)
						? remove.error.message
						: "Could not delete the run. Retry shortly."}
				</p>
			)}

			<HelpDetails summary="What am I selecting?">
				<p>
					A <strong>run</strong> is one saved anomaly-detection pass over this
					target's transactions. Each option reads{" "}
					<em>feature set · flagged of total · time</em>:
				</p>
				<ul>
					<li>
						<strong>origin:</strong> the <em>System</em> run is the auto-tuned
						run that drives scoring; a <em>Custom</em> run is an experiment you
						ran, kept separate and safe to delete.
					</li>
					<li>
						<strong>feature set:</strong> which signals are compared:{" "}
						<em>shape</em> (per-tx value, size, in/out counts, ADA moved,
						assets, time), <em>graph</em> (shared addresses), or{" "}
						<em>combined</em>.
					</li>
					<li>
						<strong>flagged of total:</strong> how many transactions the
						detectors flagged out of all that were scored.
					</li>
				</ul>
			</HelpDetails>

			<HelpDetails summary="Advanced: create an experimental run">
				<div className="space-y-4">
					<DocsCallout href={DOCS_ANOMALY}>
						This scores a <strong>separate experimental run</strong> so you can
						compare feature sets. It never changes the System run or scoring,
						and anomaly scores need interpretation (statistically unusual is not
						proof of anything). You rarely need this: to refresh scores after
						new activity or high drift, use <strong>Re-analyze</strong> on the
						contract instead, which updates the System run.
					</DocsCallout>

					<div className="flex flex-wrap items-end gap-3">
						<div className="w-40">
							<Label
								htmlFor="anomaly-feature-set"
								className="mb-1.5 block text-xs"
							>
								Feature set
							</Label>
							<FeatureSetSelect
								id="anomaly-feature-set"
								value={featureSet}
								onChange={setFeatureSet}
							/>
						</div>
						<AdminOnlyGate gated={!isAdmin}>
							<Button
								disabled={!isAdmin || detect.isPending}
								onClick={onDetect}
							>
								{detect.isPending ? "Scoring…" : scoreVerb}
							</Button>
						</AdminOnlyGate>
					</div>

					{detect.isError && (
						<p className="text-destructive text-sm">
							{isPermissionDenied(detect.error)
								? detect.error.message
								: "Detection failed. The clustering service may be slow or unavailable."}
						</p>
					)}
				</div>
			</HelpDetails>

			<Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
				<DialogContent>
					<DialogHeader>
						<DialogTitle>Delete this custom anomaly run?</DialogTitle>
						<DialogDescription>
							This removes the run and its scores. This cannot be undone. The
							system-tuned run is unaffected.
						</DialogDescription>
					</DialogHeader>
					<DialogFooter>
						<Button variant="outline" onClick={() => setConfirmDelete(false)}>
							Cancel
						</Button>
						<Button
							variant="destructive"
							disabled={remove.isPending}
							onClick={onDelete}
						>
							{remove.isPending ? "Deleting…" : "Delete"}
						</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>
		</div>
	);
}
