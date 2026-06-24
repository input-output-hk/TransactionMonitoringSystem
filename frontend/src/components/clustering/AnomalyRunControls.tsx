/**
 * Anomaly-run management bar: pick which anomaly run the table shows, run a
 * manual (custom) detection pass, and delete a custom run. The system run is
 * the canonical scored run and is the default selection; Delete is hidden for
 * it (and guarded server-side).
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
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import {
	type AnomalyRun,
	type FeatureSet,
	useDeleteAnomalyRun,
	useDetectAnomaly,
} from "@/lib/api/clustering";
import { FeatureSetSelect } from "./FeatureSetSelect";

function runLabel(r: AnomalyRun): string {
	const origin = r.origin === "system" ? "System" : "Custom";
	return `${r.feature_set} · ${origin} · ${r.n_flagged} flagged of ${r.n_points}`;
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
	const [featureSet, setFeatureSet] = useState<FeatureSet>("shape");
	const [confirmDelete, setConfirmDelete] = useState(false);
	const detect = useDetectAnomaly();
	const remove = useDeleteAnomalyRun();

	const selectedRun = runs.find((r) => r.run_id === selectedRunId) ?? null;
	const canDelete = selectedRun?.origin === "custom";

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
		<div className="space-y-2">
			<div className="flex flex-wrap items-center gap-3">
				<div className="flex items-center gap-2">
					<span className="text-muted-foreground text-sm">Run</span>
					<Select
						value={selectedRunId}
						onValueChange={onSelectRun}
						disabled={!runs.length}
					>
						<SelectTrigger className="h-9 w-[22rem] max-w-full">
							<SelectValue placeholder="No runs yet" />
						</SelectTrigger>
						<SelectContent>
							{runs.map((r) => (
								<SelectItem key={r.run_id} value={r.run_id}>
									{runLabel(r)}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
					{selectedRun && (
						<Badge
							variant={selectedRun.origin === "system" ? "outline" : "medium"}
						>
							{selectedRun.origin === "system" ? "System" : "Custom"}
						</Badge>
					)}
				</div>

				<div className="ml-auto flex items-center gap-2">
					<FeatureSetSelect
						className="h-9 w-32"
						value={featureSet}
						onChange={setFeatureSet}
					/>
					<Button disabled={detect.isPending} onClick={onDetect}>
						{detect.isPending ? "Detecting…" : "Detect anomalies"}
					</Button>
					{canDelete && (
						<Button
							variant="outline"
							disabled={remove.isPending}
							onClick={() => setConfirmDelete(true)}
						>
							Delete run
						</Button>
					)}
				</div>

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

				{detect.isError && (
					<p className="text-destructive w-full text-sm">
						Detection failed. The clustering service may be slow or unavailable.
					</p>
				)}
			</div>

			<HelpDetails summary="What am I selecting?">
				<p>
					A <strong>run</strong> is one saved anomaly-detection pass over this
					target's transactions. Each option reads{" "}
					<em>feature set · origin · flagged of total</em>:
				</p>
				<ul>
					<li>
						<strong>feature set:</strong> which signals are compared:{" "}
						<em>shape</em> (per-tx value, size, in/out counts, ADA moved,
						assets, time), <em>graph</em> (shared addresses), or{" "}
						<em>combined</em>.
					</li>
					<li>
						<strong>origin:</strong> <em>System</em> is the canonical,
						auto-tuned run that drives scoring; <em>Custom</em> is an experiment
						you ran, kept separate and safe to delete.
					</li>
					<li>
						<strong>flagged of total:</strong> how many transactions the
						detectors flagged out of all that were scored.
					</li>
				</ul>
			</HelpDetails>
		</div>
	);
}
