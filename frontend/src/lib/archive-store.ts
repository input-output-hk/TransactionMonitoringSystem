/**
 * Archive state hooks, backed by {@link archiveApi}.
 *
 * Every hook talks to the same underlying `GET /api/archive` query (or its
 * mock equivalent), scoped to the active Cardano network. Mutations invalidate
 * both the archive list AND the analysis-results queries so that archiving an
 * alert immediately removes it from the dashboard / reports views (the
 * backend already excludes archived rows via anti-join, but a stale React
 * Query cache would still show them until the next poll).
 *
 * Hooks here are the React-facing layer — callsites should not import from
 * `@/lib/api/archive` directly.
 */
import {
	keepPreviousData,
	useMutation,
	useQuery,
	useQueryClient,
} from "@tanstack/react-query";
import { useMemo } from "react";
import {
	archiveApi,
	type ArchiveBulkEntry,
	type ArchiveCreateRequest,
	type ArchiveEntry,
	type ArchiveListParams,
	type Network,
} from "@/lib/api/archive";
import { getNetwork } from "@/lib/api/fetch";

/** Re-export so consumers can keep the legacy name. */
export type ArchiveMeta = ArchiveEntry;

/** Query key for the archive list, scoped to a network. */
const archiveKey = (network: Network) => ["archive", "list", network] as const;

/** Query key for a single archive entry, scoped to (network, tx_hash). */
const archiveDetailKey = (network: Network, txHash: string) =>
	["archive", "detail", network, txHash] as const;

/** Query keys to invalidate alongside the archive list. */
const ANALYSIS_QUERY_PREFIX = ["analysis"] as const;

/** Underlying list query — shared between snapshot/full-list hooks. */
function useArchiveListQuery(params?: ArchiveListParams) {
	const network = params?.network ?? getNetwork();
	return useQuery({
		queryKey: [...archiveKey(network), params ?? {}],
		queryFn: () => archiveApi.list({ network, ...params }),
		staleTime: 30_000,
		// Keep the previous page visible while a new page/range loads, same as
		// the alerts table.
		placeholderData: keepPreviousData,
	});
}

/**
 * Single-entry query — backs both {@link useIsArchived} and {@link useArchiveMeta}.
 *
 * Hits `GET /api/archive/{tx_hash}`, which returns the entry or `null` on 404.
 * This is the deep-link path: visiting `/archive/{tx_hash}` directly works
 * even when the list query has never run (or doesn't include this entry
 * because it's past the current page).
 *
 * Both hooks share this query key, so a deep link only triggers one request.
 */
function useArchiveDetailQuery(txHash: string | undefined, network?: Network) {
	const net = network ?? getNetwork();
	return useQuery({
		queryKey: archiveDetailKey(net, txHash ?? ""),
		queryFn: () => archiveApi.get(txHash as string, net),
		enabled: !!txHash,
		staleTime: 30_000,
	});
}

/**
 * Array of archived tx_hashes for the current network. Useful for ad-hoc
 * client-side filtering; in practice the backend already anti-joins on
 * `/api/analysis/results`, so most callers don't need this anymore.
 */
export function useArchiveSnapshot(): string[] {
	const { data } = useArchiveListQuery();
	return useMemo(() => data?.data.map((e) => e.tx_hash) ?? [], [data]);
}

/**
 * Tristate "is this tx archived?". `undefined` means we don't know yet
 * (request in flight) — callsites that branch on this MUST treat
 * `undefined` as "wait" rather than collapsing it to `false`, or they'll
 * mis-redirect during the loading window.
 */
export function useIsArchived(
	txHash: string | undefined,
): boolean | undefined {
	const query = useArchiveDetailQuery(txHash);
	if (!txHash) return false;
	if (query.isPending) return undefined;
	return query.data != null;
}

/** Full archive entry for a tx_hash, or undefined if not archived / loading. */
export function useArchiveMeta(
	txHash: string | undefined,
): ArchiveEntry | undefined {
	const { data } = useArchiveDetailQuery(txHash);
	return data ?? undefined;
}

/**
 * Paginated archive list — used by ArchivePage.
 *
 * Passing `params` makes the query date/page-scoped server-side. With no
 * params it returns the full list (capped at the backend's default `limit`).
 * The returned `data` and `total` track the active filter so pagination
 * widgets can use them directly.
 */
export function useArchivedAlerts(params?: ArchiveListParams) {
	const query = useArchiveListQuery(params);
	return {
		data: query.data?.data ?? [],
		total: query.data?.total ?? 0,
		isPending: query.isPending,
		isError: query.isError,
		error: query.error,
	};
}

/**
 * Mutation: archive (mark as non-attack).
 *
 * Callers must supply the snapshot context (`network`, `tx_hash`, `note`,
 * `archived_by`) — the backend doesn't infer any of these from the alert.
 * On success we invalidate both the archive list (so /archive refetches)
 * and all analysis queries (so the dashboard drops the row).
 */
export function useArchiveMutation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (input: ArchiveCreateRequest) => archiveApi.create(input),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: ["archive"] });
			void qc.invalidateQueries({ queryKey: ANALYSIS_QUERY_PREFIX });
		},
	});
}

/**
 * Mutation: restore an archived alert.
 *
 * Identity is `(network, tx_hash)`. `network` defaults to the active one
 * from {@link getNetwork} so callsites that don't care about multi-network
 * can just pass the hash.
 */
export function useRestoreMutation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: ({ txHash, network }: { txHash: string; network?: Network }) =>
			archiveApi.remove(txHash, network),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: ["archive"] });
			void qc.invalidateQueries({ queryKey: ANALYSIS_QUERY_PREFIX });
		},
	});
}

/**
 * Mutation: bulk-import archived alerts from a CSV.
 *
 * Backend uses skip-existing semantics, so the response carries
 * `{inserted, skipped, errors}`. Wrapping it in `useMutation` ensures the
 * archive list and analysis queries are invalidated on success — without
 * this, the Archive page only picks up the new entries after the next
 * `staleTime` window or a full route remount.
 */
export function useBulkImportMutation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: ({
			entries,
			sourceLabel,
		}: {
			entries: ArchiveBulkEntry[];
			sourceLabel: string;
		}) => archiveApi.bulk(entries, sourceLabel),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: ["archive"] });
			void qc.invalidateQueries({ queryKey: ANALYSIS_QUERY_PREFIX });
		},
	});
}
