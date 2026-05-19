/** Em-dash placeholder shown in KPI cards when a value can't be computed. */
export const PLACEHOLDER_KPI = "—";

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
