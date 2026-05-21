import { useQuery } from "@tanstack/react-query";
import { fetchWithAuth, getNetwork } from "./fetch";

export type AnalysisStats = {
	total: number;
	critical_count: number;
	high_count: number;
	moderate_count: number;
	low_count: number;
	avg_max_score: number | null;
	last_analyzed_at: string;
	per_class: Record<
		string,
		{
			scored_count: number;
			avg_score: number | null;
			max_score: number | null;
		}
	>;
};

export type TransactionStats = {
	total_count: number;
	total_volume: number;
	total_fees: number;
	avg_value: number;
	first_tx: string;
	last_tx: string;
};

async function fetchAnalysisStats(): Promise<AnalysisStats> {
	const res = await fetchWithAuth(
		`/api/analysis/stats?network=${getNetwork()}`,
	);
	if (!res.ok) throw new Error(`Analysis stats failed: ${res.status}`);
	return (await res.json()) as AnalysisStats;
}

async function fetchTransactionStats(): Promise<TransactionStats> {
	const res = await fetchWithAuth(
		`/api/transactions/stats/summary?network=${getNetwork()}`,
	);
	if (!res.ok) throw new Error(`Transaction stats failed: ${res.status}`);
	return (await res.json()) as TransactionStats;
}

const POLL_MS = 5_000;

export function useAnalysisStats() {
	return useQuery({
		queryKey: ["analysis", "stats"],
		queryFn: fetchAnalysisStats,
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}

export function useTransactionStats() {
	return useQuery({
		queryKey: ["transactions", "stats"],
		queryFn: fetchTransactionStats,
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}
