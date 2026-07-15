/** Small shared formatting helpers for the clustering tables.
 *
 * ADA formatting lives in @/lib/utils/numbers (formatAdaExact for these
 * tables' precise figures, formatAdaCompact for the dashboard's K/M style)
 * and relative age in @/lib/utils/dates (formatTimeAgo with { compact: true }),
 * so there is one home per concern across the app.
 */

/** Integer with thousands separators (counts, sizes). */
export function formatInt(n: number): string {
	return Math.round(n).toLocaleString();
}

// ClickHouse toDayOfWeek: 1 = Monday … 7 = Sunday (index 0 unused).
export const WEEKDAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// A direction glyph per anomaly reason: ▲ above typical, ▼ below, • categorical.
const REASON_GLYPH: Record<string, string> = {
	high: "▲",
	low: "▼",
	unusual: "•",
	combo: "•",
};

/** Glyph for a reason direction; falls back to the categorical dot. Pure so the
 *  mapping can be unit-tested without rendering. */
export function reasonGlyph(direction: string): string {
	return REASON_GLYPH[direction] ?? "•";
}
