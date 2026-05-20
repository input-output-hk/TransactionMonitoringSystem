/**
 * In-browser mock of the archive API. Stores entries in localStorage under
 * `tms-archive-mock`. Used in dev when the real backend isn't available yet.
 *
 * Implements the exact same {@link ArchiveApi} surface as the real client,
 * so callsites are unaffected by the swap. Conflict resolution behavior
 * mirrors the documented backend contract: last-write-wins on bulk upsert.
 *
 * NOT used in production builds (see `archive.ts` for the switch).
 */
import type {
	ArchiveApi,
	ArchiveBulkResponse,
	ArchiveCreateRequest,
	ArchiveEntry,
	ArchiveListParams,
	ArchiveListResponse,
} from "./archive";

const STORAGE_KEY = "tms-archive-mock";

type Store = { entries: Record<string, ArchiveEntry> };

function readStore(): Store {
	if (typeof window === "undefined") return { entries: {} };
	try {
		const raw = window.localStorage.getItem(STORAGE_KEY);
		if (!raw) return { entries: {} };
		const parsed = JSON.parse(raw) as Partial<Store>;
		return { entries: parsed.entries ?? {} };
	} catch {
		return { entries: {} };
	}
}

function writeStore(s: Store): void {
	try {
		window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
	} catch {
		/* quota or private mode — silently ignore */
	}
}

/** Small async delay so callers don't accidentally rely on sync behavior. */
function tick<T>(value: T, ms = 50): Promise<T> {
	return new Promise((r) => setTimeout(() => r(value), ms));
}

function toEntry(
	input: ArchiveCreateRequest,
	now: string = new Date().toISOString(),
): ArchiveEntry {
	return {
		tx_hash: input.tx_hash,
		archived_at: now,
		reason: input.reason,
		notes: input.notes ?? "",
		archived_by: input.archived_by ?? "mock@local",
		attack_type_snapshot: input.attack_type_snapshot,
		severity_snapshot: input.severity_snapshot,
		risk_score_snapshot: input.risk_score_snapshot,
	};
}

export const archiveApiMock: ArchiveApi = {
	async list(params: ArchiveListParams): Promise<ArchiveListResponse> {
		const { entries } = readStore();
		const all = Object.values(entries);

		const from = params.from ? new Date(params.from).getTime() : -Infinity;
		const to = params.to ? new Date(params.to).getTime() : Infinity;
		const filtered = all
			.filter((e) => {
				const t = new Date(e.archived_at).getTime();
				return t >= from && t < to;
			})
			.sort((a, b) => b.archived_at.localeCompare(a.archived_at));

		const offset = params.offset ?? 0;
		const limit = params.limit ?? 100;
		const slice = filtered.slice(offset, offset + limit);

		return tick({
			count: slice.length,
			total: filtered.length,
			data: slice,
		});
	},

	async get(txHash: string): Promise<ArchiveEntry | null> {
		const { entries } = readStore();
		return tick(entries[txHash] ?? null);
	},

	async create(input: ArchiveCreateRequest): Promise<ArchiveEntry> {
		const store = readStore();
		const existing = store.entries[input.tx_hash];
		// Preserve original archived_at on update; only overwrite metadata
		const archivedAt = existing?.archived_at ?? new Date().toISOString();
		const entry = toEntry(input, archivedAt);
		store.entries[input.tx_hash] = entry;
		writeStore(store);
		return tick(entry);
	},

	async remove(txHash: string): Promise<void> {
		const store = readStore();
		if (store.entries[txHash]) {
			delete store.entries[txHash];
			writeStore(store);
		}
		return tick(undefined);
	},

	async bulk(inputs: ArchiveCreateRequest[]): Promise<ArchiveBulkResponse> {
		const store = readStore();
		let inserted = 0;
		let updated = 0;
		let skipped = 0;
		const errors: { row: number; reason: string }[] = [];

		const now = new Date().toISOString();
		inputs.forEach((input, idx) => {
			if (!input.tx_hash) {
				skipped++;
				errors.push({ row: idx, reason: "missing tx_hash" });
				return;
			}
			const existing = store.entries[input.tx_hash];
			if (existing) {
				// Last-write-wins: only overwrite if the incoming row is newer.
				// Mock has no source timestamp on the incoming row → always wins.
				store.entries[input.tx_hash] = toEntry(input, now);
				updated++;
			} else {
				store.entries[input.tx_hash] = toEntry(input, now);
				inserted++;
			}
		});
		writeStore(store);

		return tick({ inserted, updated, skipped, errors });
	},
};
