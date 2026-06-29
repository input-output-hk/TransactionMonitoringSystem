// React Query hooks: per-contract transaction drill-down — the latest stored
// interactions and manual per-tx verdict overrides. Public surface (re-exported
// by the barrel).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ClusterVerdict, LatestInteractionsResponse } from "../types";
import { validateLatest } from "../validation";
import { get, send } from "../transport";

/** The latest N stored interactions with a validator, newest first. The detail
 *  page gates the fetch by mounting this only on the active tab (Radix unmounts
 *  inactive panels), so no explicit enable flag is needed. */
export function useLatest(target: string | undefined, limit = 100) {
	return useQuery({
		queryKey: ["clustering", "latest", target, limit],
		queryFn: () =>
			get<LatestInteractionsResponse>(
				`/contracts/${encodeURIComponent(target!)}/latest?limit=${limit}`,
				validateLatest,
			),
		enabled: !!target,
	});
}

/** Apply a manual verdict to a single transaction (overrides cluster
 *  inheritance for this tx only; does not propagate to future txs). */
export function useLabelTx() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: {
			target: string;
			txHash: string;
			verdict: ClusterVerdict;
		}) =>
			send(
				"POST",
				`/contracts/${encodeURIComponent(a.target)}/transactions/${encodeURIComponent(a.txHash)}/label`,
				{ verdict: a.verdict },
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

export function useClearTxLabel() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { target: string; txHash: string }) =>
			send(
				"POST",
				`/contracts/${encodeURIComponent(a.target)}/transactions/${encodeURIComponent(a.txHash)}/clear-label`,
				{},
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}
