/**
 * Archive state hooks, backed by {@link archiveApi}.
 *
 * Until Phase 1 this module lived on Zustand + localStorage. Now everything
 * goes through the API client (real `/api/archive/*` in prod, localStorage
 * mock shim in dev). React Query handles caching + invalidation, so the
 * snapshot/list/derived hooks all update reactively after mutations.
 *
 * Consumers should not import from `@/lib/api/archive` directly; this module
 * is the React-facing layer.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import {
	archiveApi,
	type ArchiveCreateRequest,
	type ArchiveEntry,
} from "@/lib/api/archive";

/** Re-export of the backend entry type, used by Archive page + Detail page. */
export type ArchiveMeta = ArchiveEntry;

const ARCHIVE_QUERY_KEY = ["archive", "list"] as const;

/** Underlying list query, shared by all read hooks. */
function useArchiveListQuery() {
	return useQuery({
		queryKey: ARCHIVE_QUERY_KEY,
		// 1000 covers every dev scenario; backend already paginates if needed.
		queryFn: () => archiveApi.list({ limit: 1000 }),
		staleTime: 30_000,
	});
}

/** Array of archived tx_hashes — for client-side filtering of active lists. */
export function useArchiveSnapshot(): string[] {
	const { data } = useArchiveListQuery();
	return useMemo(() => data?.data.map((e) => e.tx_hash) ?? [], [data]);
}

/** True when the given tx_hash is currently archived. */
export function useIsArchived(txHash: string | undefined): boolean {
	const { data } = useArchiveListQuery();
	if (!txHash || !data) return false;
	return data.data.some((e) => e.tx_hash === txHash);
}

/** Full archive entry for a tx_hash, or undefined if not archived. */
export function useArchiveMeta(
	txHash: string | undefined,
): ArchiveEntry | undefined {
	const { data } = useArchiveListQuery();
	if (!txHash) return undefined;
	return data?.data.find((e) => e.tx_hash === txHash);
}

/** All archived entries, newest first. */
export function useArchivedAlerts(): ArchiveEntry[] {
	const { data } = useArchiveListQuery();
	return data?.data ?? [];
}

/**
 * Mutation: archive an alert (mark as non-attack).
 *
 * Callers should supply the snapshot fields from the current alert view so
 * the entry remains meaningful even after future re-scoring.
 */
export function useArchiveMutation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (input: ArchiveCreateRequest) => archiveApi.create(input),
		onSuccess: () => {
			qc.invalidateQueries({ queryKey: ARCHIVE_QUERY_KEY });
		},
	});
}

/** Mutation: restore an archived alert. */
export function useRestoreMutation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (txHash: string) => archiveApi.remove(txHash),
		onSuccess: () => {
			qc.invalidateQueries({ queryKey: ARCHIVE_QUERY_KEY });
		},
	});
}
