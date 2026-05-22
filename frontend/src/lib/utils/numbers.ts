/** Em-dash placeholder shown in KPI cards when a value can't be computed. */
export const PLACEHOLDER_KPI = "—";

const LOVELACE_PER_ADA = 1_000_000;

/**
 * Average transactions per minute since `firstTx`. Returns
 * {@link PLACEHOLDER_KPI} for missing/degenerate inputs.
 */
export function computeTxPerMin(
	totalCount: number | undefined,
	firstTx: string | undefined,
): string {
	if (!totalCount || !firstTx) return PLACEHOLDER_KPI;
	const elapsedMin = (Date.now() - new Date(firstTx).getTime()) / 60_000;
	if (!Number.isFinite(elapsedMin) || elapsedMin <= 0) return PLACEHOLDER_KPI;
	return Math.round(totalCount / elapsedMin).toLocaleString();
}

/**
 * Format a lovelace integer as a compact ADA string with K/M suffixes for
 * readability in tight widgets ("Latest Transactions" rows etc.).
 *
 * - ≥ 1M ADA → `12.34M ADA`
 * - ≥ 1K ADA → `3.45K ADA`
 * - ≥ 1 ADA → `7.12 ADA`
 * - sub-ADA  → `0.4521 ADA` (4 decimals so dust is still distinguishable)
 */
export function formatAda(lovelace: number | null | undefined): string {
	if (lovelace == null || !Number.isFinite(lovelace)) return PLACEHOLDER_KPI;
	const ada = lovelace / LOVELACE_PER_ADA;
	if (ada >= 1_000_000) return `${(ada / 1_000_000).toFixed(2)}M ADA`;
	if (ada >= 1_000) return `${(ada / 1_000).toFixed(2)}K ADA`;
	if (ada >= 1) return `${ada.toFixed(2)} ADA`;
	return `${ada.toFixed(4)} ADA`;
}
