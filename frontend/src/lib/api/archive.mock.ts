/**
 * In-browser mock of the archive API. Stores entries in localStorage under
 * `tms-archive-mock` keyed by `${network}:${tx_hash}`. Used in dev when the
 * real backend isn't available yet.
 *
 * Mirrors the real backend semantics:
 *  - `(network, tx_hash)` is the composite identity.
 *  - Bulk import is **skip-existing**: a local entry is never overwritten
 *    by a duplicate from the imported batch.
 *  - List join fields (`max_score`, `max_class`, `risk_band`, `analyzed_at`)
 *    are always `null` in the mock since we have no `tx_class_scores` here.
 *
 * NOT used in production builds (see `archive.ts` for the switch).
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
import { getNetwork } from "./fetch";

const STORAGE_KEY = "tms-archive-mock";

type Store = { entries: Record<string, ArchiveEntry> };

function keyOf(network: string, txHash: string): string {
	return `${network}:${txHash}`;
}

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

function makeEntry(
	input: ArchiveCreateRequest | ArchiveBulkEntry,
	source: string,
): ArchiveEntry {
	const archivedAt =
		("archived_at" in input && input.archived_at) || new Date().toISOString();
	return {
		network: input.network,
		tx_hash: input.tx_hash,
		note: input.note,
		archived_by: input.archived_by,
		archived_at: archivedAt,
		source,
		max_score: null,
		max_class: null,
		risk_band: null,
		analyzed_at: null,
	};
}

export const archiveApiMock: ArchiveApi = {
	async list(params: ArchiveListParams): Promise<ArchiveListResponse> {
		const network = params.network ?? getNetwork();
		const { entries } = readStore();
		const all = Object.values(entries).filter((e) => e.network === network);

		const fromMs = params.from ? new Date(params.from).getTime() : -Infinity;
		const toMs = params.to ? new Date(params.to).getTime() : Infinity;
		const filtered = all
			.filter((e) => {
				const t = new Date(e.archived_at).getTime();
				return t >= fromMs && t <= toMs;
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

	async get(txHash: string, network?: Network): Promise<ArchiveEntry | null> {
		const { entries } = readStore();
		return tick(entries[keyOf(network ?? getNetwork(), txHash)] ?? null);
	},

	async create(input: ArchiveCreateRequest): Promise<void> {
		const store = readStore();
		const key = keyOf(input.network, input.tx_hash);
		// Skip on existing — matches backend's 409 (treated as no-op by client).
		if (store.entries[key]) return tick(undefined);
		store.entries[key] = makeEntry(input, "local");
		writeStore(store);
		return tick(undefined);
	},

	async remove(txHash: string, network?: Network): Promise<void> {
		const store = readStore();
		const key = keyOf(network ?? getNetwork(), txHash);
		if (store.entries[key]) {
			delete store.entries[key];
			writeStore(store);
		}
		return tick(undefined);
	},

	async bulk(
		inputs: ArchiveBulkEntry[],
		sourceLabel: string,
	): Promise<ArchiveBulkResponse> {
		const store = readStore();
		let inserted = 0;
		let skipped = 0;
		const errors: string[] = [];

		const importSource = `import:${sourceLabel}`;
		const seenInBatch = new Set<string>();
		inputs.forEach((input, idx) => {
			if (!input.tx_hash) {
				skipped++;
				errors.push(`row ${idx}: missing tx_hash`);
				return;
			}
			const key = keyOf(input.network, input.tx_hash);
			// Skip-existing: never overwrite a local row, never re-insert a
			// dup within the same batch.
			if (store.entries[key] || seenInBatch.has(key)) {
				skipped++;
				return;
			}
			seenInBatch.add(key);
			store.entries[key] = makeEntry(input, importSource);
			inserted++;
		});
		writeStore(store);

		return tick({ inserted, skipped, errors });
	},

	exportUrl(params: ArchiveExportParams): string {
		const qs = new URLSearchParams();
		qs.set("network", params.network ?? getNetwork());
		if (params.from) qs.set("from", params.from);
		if (params.to) qs.set("to", params.to);
		return `/api/archive/export?${qs.toString()}`;
	},
};
