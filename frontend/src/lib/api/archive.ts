/**
 * Public entry-point for the archive API.
 *
 * Re-exports a single object `archiveApi` implementing {@link ArchiveApi}.
 * The concrete implementation is chosen at module-load time:
 *
 *  - **Production**: always the real HTTP client against `/api/archive`.
 *  - **Dev with `VITE_USE_REAL_ARCHIVE_API=true`**: real HTTP client.
 *  - **Dev otherwise (default)**: a localStorage-backed mock shim, so the
 *    UI works in isolation while the backend endpoints are being built.
 *
 * Consumers (React Query hooks, mutations, etc.) should only ever depend on
 * `ArchiveApi` — they don't need to know which implementation is active.
 *
 * See `archive.README.md` for the HTTP contract the backend must honor.
 */
import { archiveApiClient } from "./archive.client";
import { archiveApiMock } from "./archive.mock";

import type { Severity } from "@/mocks/attacks";

/* ---------- Wire format ---------- */

/** Full archive row as returned by the backend (or the mock shim). */
export type ArchiveEntry = {
	tx_hash: string;
	/** ISO datetime, set by the server when the entry is created/upserted. */
	archived_at: string;
	/** Free-text label from `ARCHIVE_REASONS` (or `"Other"`). */
	reason: string;
	/** Free-text notes from the analyst. Empty string when absent. */
	notes: string;
	/** Email/username of the user that archived. */
	archived_by: string;
	/**
	 * Snapshot of the alert at archive time. Kept so the entry stays
	 * meaningful even if the backend later re-scores the transaction
	 * into a different class.
	 */
	attack_type_snapshot: string;
	severity_snapshot: Severity;
	risk_score_snapshot: number;
};

/** Payload accepted by `POST /api/archive` and `POST /api/archive/bulk`. */
export type ArchiveCreateRequest = {
	tx_hash: string;
	reason: string;
	notes?: string;
	/**
	 * Optional from the client; backend may overwrite from auth context.
	 * Today (mock auth) we populate it with the current user's email.
	 */
	archived_by?: string;
	attack_type_snapshot: string;
	severity_snapshot: Severity;
	risk_score_snapshot: number;
};

export type ArchiveListParams = {
	/** Inclusive lower bound on `archived_at` (ISO datetime). */
	from?: string;
	/** Exclusive upper bound on `archived_at` (ISO datetime). */
	to?: string;
	limit?: number;
	offset?: number;
};

export type ArchiveListResponse = {
	count: number;
	total: number;
	data: ArchiveEntry[];
};

export type ArchiveBulkResponse = {
	inserted: number;
	updated: number;
	skipped: number;
	errors: { row: number; reason: string }[];
};

/* ---------- Public contract ---------- */

export interface ArchiveApi {
	/** Paginated list, filtered by `archived_at` range when provided. */
	list(params: ArchiveListParams): Promise<ArchiveListResponse>;

	/** Single entry by tx_hash, `null` if not archived. */
	get(txHash: string): Promise<ArchiveEntry | null>;

	/** Archive (upsert) one alert. */
	create(entry: ArchiveCreateRequest): Promise<ArchiveEntry>;

	/** Restore (delete archive entry). 204 / 404 mapped to `void`. */
	remove(txHash: string): Promise<void>;

	/** Bulk upsert — used by the Import CSV flow. */
	bulk(entries: ArchiveCreateRequest[]): Promise<ArchiveBulkResponse>;
}

/* ---------- Implementation selection ---------- */

const useReal =
	import.meta.env.PROD || import.meta.env.VITE_USE_REAL_ARCHIVE_API === "true";

/**
 * The active archive API. Same shape regardless of whether it talks to the
 * real backend or the localStorage mock.
 */
export const archiveApi: ArchiveApi = useReal
	? archiveApiClient
	: archiveApiMock;

export const isUsingMockArchive = !useReal;

/* ---------- Export helper ---------- */

export type ArchiveExportParams = Omit<ArchiveListParams, "limit" | "offset">;

/**
 * Fetch all archive entries matching `params` by paginating through the API.
 * Mirrors {@link fetchAlertsForExport} from `analysis.ts`.
 *
 * - Uses `limit=1000` per request (same cap the backend honors).
 * - Stops at `hardCap` entries (default 50k) to avoid runaway exports.
 * - `onProgress(fetched, total)` is called after each page so callers can
 *   show a progress UI.
 */
export async function fetchArchiveForExport(
	params: ArchiveExportParams,
	options?: {
		hardCap?: number;
		onProgress?: (fetched: number, total: number) => void;
	},
): Promise<ArchiveEntry[]> {
	const hardCap = options?.hardCap ?? 50_000;
	const pageSize = 1000;
	const all: ArchiveEntry[] = [];
	let offset = 0;
	let total = Infinity;

	while (offset < total && all.length < hardCap) {
		const res = await archiveApi.list({
			...params,
			limit: pageSize,
			offset,
		});
		total = res.total;
		for (const e of res.data) {
			if (all.length >= hardCap) break;
			all.push(e);
		}
		options?.onProgress?.(all.length, total);
		// Safety: backend ran out of rows before reported total.
		if (res.data.length < pageSize) break;
		offset += pageSize;
	}

	return all;
}
