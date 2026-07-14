/**
 * Validator explorer: the latest N interactions with a validator, newest first.
 * Each row's verdict is computed live against its cluster's CURRENT label, so
 * relabelling in the Clusters tab is reflected here on the next load, or
 * "Unclassified" when the tx is in no run and not yet online-scored. "Fetch &
 * classify" scores the most recent not-yet-classified txs on demand.
 */
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { HelpDetails } from "@/components/ui/help-details";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import {
	isTerminalJob,
	useClassifyNow,
	useJob,
	useLatest,
} from "@/lib/api/clustering";
import { ClusterTag, CopyHash, VerdictBadge } from "./cells";
import { formatAda, formatAge } from "./format";
import { ReasonChips } from "./ReasonChips";

const LIMIT_OPTIONS = [50, 100, 200];

export function LatestTable({ target }: { target: string }) {
	const qc = useQueryClient();
	const [limit, setLimit] = useState(100);
	const [jobId, setJobId] = useState<string | undefined>(undefined);

	const { data, isLoading, isError } = useLatest(target, limit);
	const classify = useClassifyNow();
	const job = useJob(jobId);

	// Invalidate the clustering subtree once per completed classify job so the
	// feed's verdicts (and counts elsewhere) refresh. The ref makes this fire
	// exactly once per job id, so the forced job refetch can't loop the effect.
	const handledJob = useRef<string | null>(null);
	useEffect(() => {
		const j = job.data;
		if (j && isTerminalJob(j.status) && handledJob.current !== j.job_id) {
			handledJob.current = j.job_id;
			void qc.invalidateQueries({ queryKey: ["clustering"] });
		}
	}, [job.data, qc]);

	// A classify job is in flight from submit until its job row reads terminal.
	const jobRunning = job.data
		? !isTerminalJob(job.data.status)
		: jobId !== undefined;
	const running = classify.isPending || jobRunning;
	// Surface a submit failure or a failed job; cleared implicitly when a new
	// run starts (a fresh job id replaces the failed one's data).
	const errorMsg = classify.isError
		? classify.error instanceof Error
			? classify.error.message
			: "Classification failed."
		: job.data?.status === "failed"
			? job.data.error || "Classification failed."
			: null;

	const fetchAndClassify = () => {
		classify.mutate(target, { onSuccess: (res) => setJobId(res.job_id) });
	};

	const rows = data?.transactions ?? [];
	const unclassified = rows.filter((r) => !r.classified).length;

	return (
		<div className="space-y-3">
			<div className="flex flex-wrap items-center justify-between gap-3">
				<label className="text-muted-foreground flex items-center gap-2 text-sm">
					Showing latest
					<Select
						value={String(limit)}
						onValueChange={(v) => setLimit(Number(v))}
					>
						<SelectTrigger className="h-8 w-20">
							<SelectValue />
						</SelectTrigger>
						<SelectContent>
							{LIMIT_OPTIONS.map((n) => (
								<SelectItem key={n} value={String(n)}>
									{n}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
					stored
				</label>
				<Button
					size="sm"
					disabled={running}
					onClick={fetchAndClassify}
					title="Download this validator's newest transactions and score the ones not yet classified."
				>
					{running ? "Fetching & classifying…" : "↓ Fetch newer from chain"}
				</Button>
			</div>

			<HelpDetails summary="What is this?">
				<p>
					The most recent transactions touching this validator, newest first: a
					quick way to check that recent activity looks OK. Each one shows its{" "}
					<strong>live verdict</strong>, recomputed from its cluster's current
					label:
				</p>
				<ul>
					<li>
						<strong>unclassified:</strong> not in any cluster run and not yet
						scored; hit <strong>Fetch newer from chain</strong> to score the
						recent ones.
					</li>
					<li>
						label its cluster <strong>benign</strong>/<strong>malicious</strong>{" "}
						in the Clusters tab and the verdict follows, retroactively and going
						forward.
					</li>
					<li>
						no label: <strong>anomaly</strong> if the detectors flag it (votes ≥
						2), else <strong>normal</strong>.
					</li>
				</ul>
				<p>
					Two separate controls: <strong>Showing latest N stored</strong> only
					changes how many already-downloaded transactions are listed (no
					network call); <strong>Fetch newer from chain</strong> downloads
					transactions newer than what's stored and classifies them.
				</p>
			</HelpDetails>

			{errorMsg && <p className="text-destructive text-sm">{errorMsg}</p>}

			{isLoading ? (
				<p className="text-muted-foreground text-sm">Loading interactions…</p>
			) : isError ? (
				<p className="text-destructive text-sm">
					Failed to load the latest interactions.
				</p>
			) : !rows.length ? (
				<p className="text-muted-foreground text-sm">
					No transactions stored yet. Use “Fetch newer from chain”.
				</p>
			) : (
				<>
					<p className="text-muted-foreground text-xs">
						{rows.length} most recent, newest first
						{unclassified > 0 && ` · ${unclassified} not yet classified`}.
						Verdicts track each cluster's current label.
					</p>
					<Table>
						<TableHeader>
							<TableRow className="hover:bg-transparent">
								<TableHead>Transaction</TableHead>
								<TableHead>Age</TableHead>
								<TableHead>Verdict</TableHead>
								<TableHead>Cluster</TableHead>
								<TableHead className="text-right">Votes</TableHead>
								<TableHead className="text-right">Fee (₳)</TableHead>
								<TableHead className="text-right">Out (₳)</TableHead>
								<TableHead className="text-right">In/Out</TableHead>
								<TableHead className="text-right">Assets</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{rows.map((r) => (
								<TableRow key={r.tx_hash}>
									<TableCell>
										<CopyHash hash={r.tx_hash} />
										<ReasonChips reasons={r.reasons} />
									</TableCell>
									<TableCell
										className="text-muted-foreground"
										title={r.block_time}
									>
										{formatAge(r.block_time)}
									</TableCell>
									<TableCell>
										{/* A not-yet-classified row reads as Unclassified, same as a
										    null verdict. */}
										<VerdictBadge verdict={r.classified ? r.verdict : null} />
									</TableCell>
									<TableCell>
										{r.cluster_id === null ? (
											<span className="text-muted-foreground">—</span>
										) : (
											<ClusterTag clusterId={r.cluster_id} />
										)}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{r.classified ? r.votes : "—"}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{formatAda(r.fees, 0)}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{formatAda(r.total_output_lovelace, 0)}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{r.input_count}/{r.output_count}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{r.distinct_assets}
									</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</>
			)}
		</div>
	);
}
