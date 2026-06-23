/** Small shared formatting helpers for the clustering tables. */
import { LOVELACE_PER_ADA } from "@/lib/utils/numbers";

/** Lovelace → plain ADA string. `digits` controls max fraction digits. Unlike
 *  the dashboard's K/M-suffixed `formatAda`, these tables want exact figures. */
export function formatAda(lovelace: number, digits = 2): string {
	return (lovelace / LOVELACE_PER_ADA).toLocaleString(undefined, {
		maximumFractionDigits: digits,
	});
}

/** Integer with thousands separators (counts, sizes). */
export function formatInt(n: number): string {
	return Math.round(n).toLocaleString();
}

/**
 * Compact relative age for a ClickHouse `block_time` ("YYYY-MM-DD HH:MM:SS",
 * UTC): "12s ago" / "5m ago" / "3h ago" / "2d ago". Returns the raw string if
 * it can't be parsed. Pair with a `title` of the absolute timestamp.
 */
export function formatAge(blockTime: string): string {
	const t = Date.parse(`${blockTime.replace(" ", "T")}Z`);
	if (Number.isNaN(t)) return blockTime;
	const secs = Math.max(0, (Date.now() - t) / 1000);
	if (secs < 60) return `${Math.floor(secs)}s ago`;
	if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
	if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
	return `${Math.floor(secs / 86400)}d ago`;
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
