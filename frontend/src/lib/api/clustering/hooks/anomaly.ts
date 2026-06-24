// React Query hooks: anomaly runs (list, top candidates, manual detect, delete).
// Public surface (re-exported by the barrel).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { AnomalyRun, AnomalyTopResponse, FeatureSet } from "../types";
import { anomalyRunItem, arrayOf, validateAnomalyTop } from "../validation";
import { get, send } from "../transport";

export function useAnomalyRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "anomaly-runs", target],
		queryFn: () =>
			get<AnomalyRun[]>(
				`/anomaly-runs?target=${encodeURIComponent(target!)}`,
				arrayOf("/anomaly-runs", anomalyRunItem),
			),
		enabled: !!target,
	});
}

export function useTopAnomalies(runId: string | undefined, limit = 100) {
	return useQuery({
		queryKey: ["clustering", "anomaly-top", runId, limit],
		queryFn: () =>
			get<AnomalyTopResponse>(
				`/anomaly-runs/${runId}/top?limit=${limit}`,
				validateAnomalyTop,
			),
		enabled: !!runId,
	});
}

/** Run a manual anomaly detection pass. Creates a CUSTOM anomaly run. */
export function useDetectAnomaly() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (body: { target: string; feature_set: FeatureSet }) =>
			send<AnomalyRun>("POST", "/anomaly", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

/** Delete a custom anomaly run and its scores. System runs are guarded
 *  server-side; the UI only offers this for custom runs. */
export function useDeleteAnomalyRun() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (runId: string) =>
			send<{ deleted: boolean }>(
				"DELETE",
				`/anomaly-runs/${encodeURIComponent(runId)}`,
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}
