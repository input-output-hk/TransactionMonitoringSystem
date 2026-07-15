/** Em-dash placeholder shown in KPI cards when a value can't be computed. */
export const PLACEHOLDER_KPI = "—";

/** 1 ADA = 1,000,000 lovelace (Cardano protocol fixed denomination). */
export const LOVELACE_PER_ADA = 1_000_000;

/**
 * Format a lovelace integer as a compact ADA string with K/M suffixes for
 * readability in tight widgets ("Latest Transactions" rows etc.).
 *
 * - ≥ 1M ADA → `12.34M ADA`
 * - ≥ 1K ADA → `3.45K ADA`
 * - ≥ 1 ADA → `7.12 ADA`
 * - sub-ADA  → `0.4521 ADA` (4 decimals so dust is still distinguishable)
 */
export function formatAdaCompact(lovelace: number | null | undefined): string {
	if (lovelace == null || !Number.isFinite(lovelace)) return PLACEHOLDER_KPI;
	const ada = lovelace / LOVELACE_PER_ADA;
	if (ada >= 1_000_000) return `${(ada / 1_000_000).toFixed(2)}M ADA`;
	if (ada >= 1_000) return `${(ada / 1_000).toFixed(2)}K ADA`;
	if (ada >= 1) return `${ada.toFixed(2)} ADA`;
	return `${ada.toFixed(4)} ADA`;
}

/**
 * Format a lovelace integer as an EXACT ADA figure with thousands separators
 * (no K/M rounding). For the clustering/detail tables that want precise values
 * rather than the dashboard's compact style. `digits` caps fraction digits.
 */
export function formatAdaExact(lovelace: number, digits = 2): string {
	return (lovelace / LOVELACE_PER_ADA).toLocaleString(undefined, {
		maximumFractionDigits: digits,
	});
}
