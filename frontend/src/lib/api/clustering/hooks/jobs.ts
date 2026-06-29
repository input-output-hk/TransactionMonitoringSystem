// React Query hooks: background onboarding/reprocess jobs (poll one or all,
// stopping once terminal). Public surface (re-exported by the barrel).
import { useQuery } from "@tanstack/react-query";

import type { Job, JobStatus } from "../types";
import { arrayOf, jobItem } from "../validation";
import { get } from "../transport";

/** Terminal job statuses — a job in either state is finished and its actions can
 *  safely re-run. Shared by the poll-stop logic and the stage presentation. */
const TERMINAL_JOB_STATUS: ReadonlySet<JobStatus> = new Set<JobStatus>([
	"done",
	"failed",
]);

export function isTerminalJob(status: JobStatus): boolean {
	return TERMINAL_JOB_STATUS.has(status);
}

/** Poll a single job while it is running; stop polling once it is terminal. */
export function useJob(jobId: string | undefined, pollMs = 2500) {
	return useQuery({
		queryKey: ["clustering", "job", jobId],
		queryFn: () => get<Job>(`/jobs/${jobId}`, jobItem),
		enabled: !!jobId,
		// `query.state.data` is the last fetched job; keep polling until terminal.
		refetchInterval: (query) =>
			query.state.data && isTerminalJob(query.state.data.status)
				? false
				: pollMs,
	});
}

/** All known jobs (newest first), polled so card status badges stay live. */
export function useJobs(pollMs = 2500, enabled = true) {
	return useQuery({
		queryKey: ["clustering", "jobs"],
		queryFn: () => get<Job[]>("/jobs", arrayOf("/jobs", jobItem)),
		refetchInterval: pollMs,
		enabled,
	});
}
