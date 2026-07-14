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

async function fetchLatestTransactions(
	limit: number,
): Promise<TransactionRow[]> {
	const qs = new URLSearchParams();
	qs.set("network", getNetwork());
	qs.set("limit", String(limit));
	const res = await fetchWithAuth(`/api/v1/transactions?${qs.toString()}`);
	if (!res.ok) throw new Error(`Latest transactions failed: ${res.status}`);
	return (await res.json()) as TransactionRow[];
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
