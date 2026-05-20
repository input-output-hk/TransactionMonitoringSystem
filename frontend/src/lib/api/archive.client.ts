/**
 * Real HTTP client for the archive API. Hits `/api/archive/*` on the backend.
 *
 * Backend contract: see `archive.README.md`.
 *
 * This file is consumed by `archive.ts` only — callsites should import
 * `archiveApi` from there, not from here directly.
 */
import type {
	ArchiveApi,
	ArchiveBulkResponse,
	ArchiveCreateRequest,
	ArchiveEntry,
	ArchiveListParams,
	ArchiveListResponse,
} from "./archive";

async function json<T>(res: Response): Promise<T> {
	if (!res.ok) {
		const body = await res.text().catch(() => "");
		throw new Error(
			`Archive API ${res.status}${body ? `: ${body.slice(0, 200)}` : ""}`,
		);
	}
	return (await res.json()) as T;
}

export const archiveApiClient: ArchiveApi = {
	async list(params: ArchiveListParams): Promise<ArchiveListResponse> {
		const qs = new URLSearchParams();
		if (params.from) qs.set("from", params.from);
		if (params.to) qs.set("to", params.to);
		if (params.limit != null) qs.set("limit", String(params.limit));
		if (params.offset != null) qs.set("offset", String(params.offset));
		const res = await fetch(`/api/archive?${qs.toString()}`);
		return json<ArchiveListResponse>(res);
	},

	async get(txHash: string): Promise<ArchiveEntry | null> {
		const res = await fetch(`/api/archive/${encodeURIComponent(txHash)}`);
		if (res.status === 404) return null;
		return json<ArchiveEntry>(res);
	},

	async create(entry: ArchiveCreateRequest): Promise<ArchiveEntry> {
		const res = await fetch("/api/archive", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(entry),
		});
		return json<ArchiveEntry>(res);
	},

	async remove(txHash: string): Promise<void> {
		const res = await fetch(`/api/archive/${encodeURIComponent(txHash)}`, {
			method: "DELETE",
		});
		if (res.status === 404 || res.status === 204) return;
		if (!res.ok) {
			throw new Error(`Archive remove failed: ${res.status}`);
		}
	},

	async bulk(entries: ArchiveCreateRequest[]): Promise<ArchiveBulkResponse> {
		const res = await fetch("/api/archive/bulk", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ entries }),
		});
		return json<ArchiveBulkResponse>(res);
	},
};
