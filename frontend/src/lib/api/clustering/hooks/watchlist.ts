// React Query hooks: the watched-contract registry (list, add, delete, rename,
// identify, manual reclassify). Public surface (re-exported by the barrel).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Contract, IdentifyResult } from "../types";
import { validateContracts, validateIdentify } from "../validation";
import { get, send } from "../transport";

const CONTRACTS_KEY = ["clustering", "contracts"] as const;

export function useContracts(pollMs = 10_000, enabled = true) {
	return useQuery({
		queryKey: CONTRACTS_KEY,
		queryFn: () => get<Contract[]>("/contracts", validateContracts),
		refetchInterval: pollMs,
		// Let the page hold the query off until it knows clustering is enabled, so
		// a clustering-disabled deployment never polls /api/clustering/*.
		enabled,
	});
}

/** Upper bound the API accepts for max_txs (MAX_TXS_CAP server-side). */
export const MAX_TXS_CAP = 50_000;

export function useAddContract() {
	const qc = useQueryClient();
	return useMutation({
		// max_txs: how many of the most recent txs to import (omit = the full
		// configured window). reprocess: force a full re-cluster on re-add.
		mutationFn: (body: {
			target: string;
			label?: string;
			max_txs?: number;
			reprocess?: boolean;
		}) => send<{ job_id: string }>("POST", "/contracts", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

export function useDeleteContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ deleted: boolean }>(
				"DELETE",
				`/contracts/${encodeURIComponent(target)}`,
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

/** Rename a watched contract (its display label). */
export function useRenameContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { target: string; label: string }) =>
			send<Contract>("PATCH", `/contracts/${encodeURIComponent(a.target)}`, {
				label: a.label,
			}),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

/** Live, offline identification of a (debounced) typed target. Enabled only on
 *  a non-empty target; identification is deterministic so it caches for a while.
 *  The caller debounces the `target` it passes in. */
export function useIdentify(target: string) {
	const trimmed = target.trim();
	return useQuery({
		queryKey: ["clustering", "identify", trimmed],
		queryFn: () =>
			get<IdentifyResult>(
				`/registry/identify?target=${encodeURIComponent(trimmed)}`,
				validateIdentify,
			),
		enabled: trimmed.length > 0,
		staleTime: 5 * 60_000,
		retry: false,
	});
}

/** Force an immediate incremental re-classify (the auto feed supersedes this;
 *  exposed as a manual "refresh now" for analysts). */
export function useClassifyNow() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ job_id: string }>(
				"POST",
				`/contracts/${encodeURIComponent(target)}/classify-new`,
				{},
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}
