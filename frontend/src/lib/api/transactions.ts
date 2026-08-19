/**
 * Read-only hooks for raw transaction/block data used by the dashboard
 * "Latest …" widgets.
 *
 * The schema has no `blocks` table — `/api/v1/transactions/blocks/recent`
 * aggregates by `block_height` on the backend. See `backend/app/api/transactions.py`.
 */
import { useQuery } from "@tanstack/react-query";
import { fetchWithAuth, getNetwork } from "./fetch";

/* ---------- Wire format ---------- */

export type TransactionRow = {
	tx_hash: string;
	slot: number | null;
	block_height: number | null;
	block_hash: string | null;
	block_index: number | null;
	timestamp: string; // ISO, naive UTC from ClickHouse
	fee: number;
	deposit: number | null;
	input_count: number;
	output_count: number;
	total_input_value: number | null;
	total_output_value: number;
	addresses: string[];
};

export type RecentBlock = {
	block_height: number;
	block_hash: string;
	timestamp: string; // ISO, naive UTC from ClickHouse
	tx_count: number;
	total_output_value: number;
};

/* ---------- Fetchers ---------- */

/** Shared list envelope: every /api/v1 list endpoint returns {count,total,data}
 * (total is null on cursor-paginated feeds like this one). */
type ListResponse<T> = { count: number; total: number | null; data: T[] };

async function fetchLatestTransactions(
	limit: number,
): Promise<TransactionRow[]> {
	const qs = new URLSearchParams();
	qs.set("network", getNetwork());
	qs.set("limit", String(limit));
	const res = await fetchWithAuth(`/api/v1/transactions?${qs.toString()}`);
	if (!res.ok) throw new Error(`Latest transactions failed: ${res.status}`);
	const json = (await res.json()) as ListResponse<TransactionRow>;
	return json.data;
}

async function fetchRecentBlocks(limit: number): Promise<RecentBlock[]> {
	const qs = new URLSearchParams();
	qs.set("network", getNetwork());
	qs.set("limit", String(limit));
	const res = await fetchWithAuth(
		`/api/v1/transactions/blocks/recent?${qs.toString()}`,
	);
	if (!res.ok) throw new Error(`Recent blocks failed: ${res.status}`);
	return (await res.json()) as RecentBlock[];
}

/* ---------- Hooks ---------- */

// Cardano slot time is 1s, but block production averages ~20s. Polling at
// 15s keeps the widgets feeling live without burning rate-limit budget.
const POLL_MS = 15_000;

export function useLatestTransactions(limit = 5) {
	return useQuery({
		queryKey: ["transactions", "latest", limit],
		queryFn: () => fetchLatestTransactions(limit),
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}

export function useRecentBlocks(limit = 5) {
	return useQuery({
		queryKey: ["transactions", "blocks", "recent", limit],
		queryFn: () => fetchRecentBlocks(limit),
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}

/* ---------- Single-transaction detail ---------- */

/** One node of a decoded Plutus datum or redeemer tree. */
export type DatumNode = {
	kind:
		| "constructor"
		| "list"
		| "map"
		| "map_entry"
		| "bytes"
		| "int"
		| "text"
		| "truncated"
		| "unknown";
	/**
	 * Constructor index, for `kind: "constructor"`.
	 *
	 * Deliberately NOT named `constructor`: that is `Object.prototype.constructor`
	 * in JS, so a `Partial<DatumNode>` resolves the field to `Function` and any
	 * structural check against it fails in a way that reads as a type-system bug.
	 * The backend field is named to match.
	 */
	constructor_index: number | null;
	/** Hex for bytes, decimal for int, the string for text. Null on structural nodes. */
	value: string | null;
	/** UTF-8 reading of a bytes leaf when it decodes cleanly and is printable. */
	text: string | null;
	children: DatumNode[];
};

export type DecodedDatum = {
	root: DatumNode | null;
	encoding: "cbor_hex" | "plutus_json" | "none";
	/** The depth or node budget elided part of the tree. */
	truncated: boolean;
	/** Why decoding failed. Undecodable is information, so render it, don't hide it. */
	error: string | null;
};

export type OutputDatum = {
	output_index: number;
	datum_hash: string | null;
	/** The payload came from the witness preimage map rather than inline. */
	resolved_from_witness: boolean;
	/** Null means not measurable, never "measured and empty". */
	size_bytes: number | null;
	hex: string | null;
	structure: DecodedDatum | null;
};

/**
 * Pointer index for a redeemer whose payload does not state one. Mirrors
 * `REDEEMER_INDEX_UNKNOWN` in `backend/app/analysis/features.py`; distinct from
 * any real index (a ledger pointer is always >= 0) so "not stated" is
 * distinguishable from index 0.
 */
export const REDEEMER_INDEX_UNKNOWN = -1;

export type TransactionRedeemer = {
	purpose: string;
	index: number;
	hex: string | null;
	structure: DecodedDatum | null;
	memory_units: number;
	cpu_units: number;
};

export type TxInput = {
	tx_hash: string;
	index: number;
	address: string;
	amount: number;
	/** `{"policy.assetname": quantity}`, or null when the output holds only ADA. */
	assets: Record<string, number> | null;
	is_reference: boolean;
	is_collateral: boolean;
	is_unspent_attempt: boolean;
};

export type TxOutput = {
	index: number;
	address: string;
	amount: number;
	assets: Record<string, number> | null;
	is_collateral: boolean;
};

export type TransactionDetail = TransactionRow & {
	inputs: TxInput[];
	outputs: TxOutput[];
	metadata: Record<string, unknown> | null;
	datums: OutputDatum[];
	redeemers: TransactionRedeemer[];
	/**
	 * False when the raw Ogmios payload could not be recovered, so empty
	 * datums/redeemers mean "unknown", not "none". The UI must show the
	 * difference: on a flagged transaction, absence of evidence is not evidence
	 * of absence.
	 */
	script_data_available: boolean;
};

async function fetchTransactionDetail(
	txHash: string,
): Promise<TransactionDetail | null> {
	const qs = new URLSearchParams({ network: getNetwork() });
	const res = await fetchWithAuth(
		`/api/v1/transactions/${txHash}?${qs.toString()}`,
	);
	// A scored alert can outlive its transaction row (retention), so a missing
	// transaction is an expected empty state, not an error.
	if (res.status === 404) return null;
	if (!res.ok) {
		throw new Error(`Transaction detail request failed: ${res.status}`);
	}
	return (await res.json()) as TransactionDetail;
}

/**
 * Chain-level detail for one transaction: inputs, outputs, values, datums and
 * redeemers. Not polled: a confirmed transaction is immutable, so one fetch per
 * open is enough.
 */
export function useTransactionDetail(
	txHash: string | undefined,
	enabled = true,
) {
	return useQuery({
		queryKey: ["transactions", "detail", txHash],
		queryFn: () => fetchTransactionDetail(txHash as string),
		enabled: !!txHash && enabled,
		staleTime: Infinity,
	});
}
