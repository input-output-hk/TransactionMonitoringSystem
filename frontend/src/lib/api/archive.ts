/**
 * Public entry-point for the archive API. Mirrors the backend contract in
 * `backend/app/api/archive.py` + `backend/app/models/archive.py`.
 *
 * Two implementations are wired behind the same {@link ArchiveApi} surface:
 *
 *  - **Default everywhere**: real HTTP client against `/api/v1/archive/*`
 *    ({@link archive.client.ts}).
 *  - **Dev with `VITE_USE_MOCK_ARCHIVE_API=true`**: localStorage-backed mock
 *    shim ({@link archive.mock.ts}). Opt-in only — used for offline work
 *    when the backend isn't reachable. Production builds ignore the flag
 *    and always use the real client.
 *
 * Backend specifics:
 *  - Archive identity is `(network, tx_hash)` — every call carries `network`.
 *  - Notes live in a single free-text `note` field. The Delete dialog in the
 *    Detail page composes its "reason" + free notes into this one string.
 *  - Bulk import is **skip-existing**: existing rows are never overwritten
 *    (response shape has `inserted`/`skipped`, no `updated`).
 *  - Export CSV is **server-side**: callers fetch it via {@link ArchiveApi.download}
 *    and hand the Blob to a programmatic anchor click — there's no client
 *    paginated fetch.
 */
import { archiveApiClient } from "./archive.client";
import { archiveApiMock } from "./archive.mock";

import type { Severity } from "@/lib/attacks";

/* ---------- Wire format ---------- */

/** Cardano network identifier. Same enum as the backend `NetworkType`. */
export type Network = "mainnet" | "preprod" | "preview";

/** Backend response row for `GET /api/v1/archive`. */
export type ArchiveEntry = {
	network: Network;
	tx_hash: string;
	/** Free-text "reason + notes" composed by the UI on archive. */
	note: string;
	archived_by: string;
	/** ISO datetime, set by the server. */
	archived_at: string;
	/** Origin tag: `"local"` or `"import:<source_label>"`. */
	source: string;
	/* ---- joined from tx_class_scores (null when archive came from CSV import
	   for a tx this instance has never observed locally) ---- */
	max_score: number | null;
	max_class: string | null;
	risk_band: string | null;
	analyzed_at: string | null;
};

/** Payload for `POST /api/v1/archive`. */
export type ArchiveCreateRequest = {
	network: Network;
	tx_hash: string;
	note: string;
	archived_by: string;
};

/** Entry inside a bulk-import body. */
export type ArchiveBulkEntry = ArchiveCreateRequest & {
	/** Original archive timestamp from the source instance, if known. */
	archived_at?: string;
	/** Original `source` tag from the exported CSV (ignored, kept for round-trip). */
	source?: string;
};

export type ArchiveListParams = {
	network?: Network;
	/** Inclusive lower bound on `archived_at` (ISO datetime). */
	from?: string;
	/** Inclusive upper bound on `archived_at` (ISO datetime). */
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
	skipped: number;
	errors: string[];
};

export type ArchiveExportParams = {
	network?: Network;
	from?: string;
	to?: string;
};

/* ---------- Public contract ---------- */

export interface ArchiveApi {
	/** Paginated list. Date range bounds are inclusive on both ends. */
	list(params: ArchiveListParams): Promise<ArchiveListResponse>;

	/** Single entry by tx_hash, `null` if not archived. */
	get(txHash: string, network?: Network): Promise<ArchiveEntry | null>;

	/** Archive one alert. Backend returns 409 on duplicate `(network, tx_hash)`. */
	create(entry: ArchiveCreateRequest): Promise<void>;

	/** Restore (hard-delete) the archive row. 204/404 both map to `void`. */
	remove(txHash: string, network?: Network): Promise<void>;

	/**
	 * Bulk-upsert with **skip-existing** semantics (never overwrites).
	 * `sourceLabel` tags the origin instance on inserted rows.
	 */
	bulk(
		entries: ArchiveBulkEntry[],
		sourceLabel: string,
	): Promise<ArchiveBulkResponse>;

	/**
	 * Download the CSV for the current params and return it as a Blob plus
	 * the suggested filename (parsed from the server's Content-Disposition).
	 *
	 * Goes through `fetchWithAuth` (session cookie) rather than a plain
	 * `<a download href>` so an expired session surfaces as a typed 401
	 * (UnauthorizedError → login redirect) instead of a silently failed
	 * navigation, and so the server's Content-Disposition filename can be
	 * parsed. The page wires this blob to a programmatic anchor click to
	 * trigger the browser download.
	 */
	download(params: ArchiveExportParams): Promise<{
		blob: Blob;
		filename: string;
	}>;
}

/* ---------- Implementation selection ---------- */

// Real backend by default. The mock shim is opt-in via env var and only
// honored in dev builds — production always talks to the real backend so a
// stray env var can't silently switch a deployment to localStorage.
const useMock =
	import.meta.env.DEV &&
	import.meta.env.VITE_USE_MOCK_ARCHIVE_API === "true";

export const archiveApi: ArchiveApi = useMock
	? archiveApiMock
	: archiveApiClient;

export const isUsingMockArchive = useMock;

/* ---------- Type re-exports for downstream consumers ---------- */

export type { Severity };
