/** Human-readable stage text + lookup helpers for clustering background jobs. */
import { type Job, isTerminalJob, useJobs } from "@/lib/api/clustering";

const STAGE_LABEL: Record<string, string> = {
	queued: "Queued…",
	checking: "Checking address & fetching metadata…",
	downloading: "Downloading transactions…",
	clustering: "Clustering…",
	scoring: "Scoring anomalies…",
	done: "Done",
	failed: "Failed",
};

// Classify jobs reuse the downloading/scoring stages but mean something
// different from a full onboard, so relabel those two for clarity.
const CLASSIFY_STAGE_LABEL: Record<string, string> = {
	downloading: "Fetching new transactions…",
	scoring: "Classifying new transactions…",
};

export function stageLabel(job: Job): string {
	if (job.kind === "classify" && CLASSIFY_STAGE_LABEL[job.status]) {
		return CLASSIFY_STAGE_LABEL[job.status];
	}
	return STAGE_LABEL[job.status] ?? job.status;
}

/** Newest job for `target`, or null. Jobs come newest-first from the sidecar;
 *  we still scan defensively rather than assuming order. */
export function latestJobForTarget(
	jobs: Job[] | undefined,
	target: string,
): Job | null {
	if (!jobs?.length) return null;
	let best: Job | null = null;
	for (const j of jobs) {
		if (j.target !== target) continue;
		if (!best || j.created_at > best.created_at) best = j;
	}
	return best;
}

/** The target's latest job if it is currently in-flight (non-terminal), else
 *  null. Reads the shared jobs poll, so callers share one request. */
export function useActiveJob(target: string): Job | null {
	const { data } = useJobs();
	const job = latestJobForTarget(data, target);
	return job && !isTerminalJob(job.status) ? job : null;
}
