/**
 * Live job progress for a watched contract. Cards poll the shared jobs list
 * (one request, deduped across cards by the shared query key) and render the
 * running stage / failure line for their target. Job lookup helpers live in
 * `jobStage.ts`; this file exports only the component so fast-refresh works.
 */
import { type Job, isTerminalJob } from "@/lib/api/clustering";
import { stageLabel } from "./jobStage";

/** Inline progress/failure line for a target's latest job. Renders nothing when
 *  there is no job or the job finished cleanly. */
export function JobProgress({ job }: { job: Job | null }) {
	if (!job) return null;
	const running = !isTerminalJob(job.status);
	if (running) {
		return (
			<div className="text-muted-foreground flex items-center gap-2 text-xs">
				<span className="bg-brand inline-block h-2 w-2 animate-pulse rounded-full" />
				<span>{job.stage_detail || stageLabel(job)}</span>
			</div>
		);
	}
	if (job.status === "failed") {
		return (
			<div className="text-destructive text-xs">
				{job.error || "Last job failed."}
			</div>
		);
	}
	return null;
}
