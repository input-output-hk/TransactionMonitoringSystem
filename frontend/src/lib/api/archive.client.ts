/**
 * Real HTTP client for the archive API. Hits `/api/archive/*` on the backend.
 *
 * Backend contract: `backend/app/api/archive.py`. Identity is
 * `(network, tx_hash)` — every call carries the active network from
 * {@link getNetwork}.
 *
 * Consumed by `archive.ts` only — callsites import `archiveApi` from there.
 */
import type {
	ArchiveApi,
	ArchiveBulkEntry,
	ArchiveBulkResponse,
	ArchiveCreateRequest,
	ArchiveEntry,
	ArchiveExportParams,
	ArchiveListParams,
	ArchiveListResponse,
	Network,
} from "./archive";
import { fetchWithAuth, getNetwork } from "./fetch";

async function json<T>(res: Response): Promise<T> {
	if (!res.ok) {
		const body = await res.text().catch(() => "");
		throw new Error(
			`Archive API ${res.status}${body ? `: ${body.slice(0, 200)}` : ""}`,
		);
	}
	return (await res.json()) as T;
}

function buildArchiveQuery(params: {
	network?: Network;
	from?: string;
	to?: string;
	limit?: number;
	offset?: number;
}): URLSearchParams {
	const qs = new URLSearchParams();
	qs.set("network", params.network ?? getNetwork());
	if (params.from) qs.set("from", params.from);
	if (params.to) qs.set("to", params.to);
	if (params.limit != null) qs.set("limit", String(params.limit));
	if (params.offset != null) qs.set("offset", String(params.offset));
	return qs;
}

export const archiveApiClient: ArchiveApi = {
	async list(params: ArchiveListParams): Promise<ArchiveListResponse> {
		const qs = buildArchiveQuery(params);
		const res = await fetchWithAuth(`/api/archive?${qs.toString()}`);
		return json<ArchiveListResponse>(res);
	},

	async get(txHash: string, network?: Network): Promise<ArchiveEntry | null> {
		const qs = new URLSearchParams();
		qs.set("network", network ?? getNetwork());
		const res = await fetchWithAuth(
			`/api/archive/${encodeURIComponent(txHash)}?${qs.toString()}`,
		);
		if (res.status === 404) return null;
		return json<ArchiveEntry>(res);
	},

	async create(entry: ArchiveCreateRequest): Promise<void> {
		const res = await fetchWithAuth("/api/archive", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(entry),
		});
		// 201 success, 409 means it's already archived — treat as no-op (idempotent).
		if (res.status === 409) return;
		if (!res.ok) {
			const body = await res.text().catch(() => "");
			throw new Error(
				`Archive create ${res.status}${body ? `: ${body.slice(0, 200)}` : ""}`,
			);
		}
	},

	async remove(txHash: string, network?: Network): Promise<void> {
		const qs = new URLSearchParams();
		qs.set("network", network ?? getNetwork());
		const res = await fetchWithAuth(
			`/api/archive/${encodeURIComponent(txHash)}?${qs.toString()}`,
			{ method: "DELETE" },
		);
		// 204 ok, 404 means it wasn't archived — both acceptable on restore.
		if (res.status === 204 || res.status === 404) return;
		if (!res.ok) {
			throw new Error(`Archive remove failed: ${res.status}`);
		}
	},

	async bulk(
		entries: ArchiveBulkEntry[],
		sourceLabel: string,
	): Promise<ArchiveBulkResponse> {
		const res = await fetchWithAuth("/api/archive/bulk", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ entries, source_label: sourceLabel }),
		});
		return json<ArchiveBulkResponse>(res);
	},

	async download(
		params: ArchiveExportParams,
	): Promise<{ blob: Blob; filename: string }> {
		const qs = new URLSearchParams();
		qs.set("network", params.network ?? getNetwork());
		if (params.from) qs.set("from", params.from);
		if (params.to) qs.set("to", params.to);
		const res = await fetchWithAuth(`/api/archive/export?${qs.toString()}`);
		if (!res.ok) {
			const body = await res.text().catch(() => "");
			throw new Error(
				`Archive export ${res.status}${body ? `: ${body.slice(0, 200)}` : ""}`,
			);
		}
		const blob = await res.blob();
		// Try Content-Disposition first; fall back to a sensible default if the
		// header is missing or the regex doesn't match (e.g. behind a proxy
		// that strips headers).
		const cd = res.headers.get("Content-Disposition") ?? "";
		const match = cd.match(/filename="?([^";]+)"?/i);
		const filename =
			match?.[1] ??
			`tms-archive-${params.network ?? getNetwork()}-${params.from?.slice(0, 10) ?? "all"}-${params.to?.slice(0, 10) ?? "all"}.csv`;
		return { blob, filename };
	},
};
