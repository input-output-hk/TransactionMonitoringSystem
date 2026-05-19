/**
 * Date / datetime helpers shared across pages.
 *
 * All formatters/parsers here treat strings as plain ISO or `yyyy-mm-dd`
 * to keep things deterministic; no locale-dependent parsing.
 */

/** Today's date in `yyyy-mm-dd` form (local TZ). */
export function todayISODate(): string {
	return new Date().toISOString().slice(0, 10);
}

/** Date N days ago in `yyyy-mm-dd` form. */
export function nDaysAgoISODate(n: number): string {
	const d = new Date();
	d.setDate(d.getDate() - n);
	return d.toISOString().slice(0, 10);
}

/** Default Reports range start: 30 days ago. */
export function defaultStart(): string {
	return nDaysAgoISODate(30);
}

/** Default Reports range end: today. */
export function defaultEnd(): string {
	return todayISODate();
}

/** Convert a `yyyy-mm-dd` date to an ISO datetime at start of day (local TZ). */
export function startOfDayISO(date: string): string | undefined {
	if (!date) return undefined;
	return new Date(`${date}T00:00:00`).toISOString();
}

/** Exclusive upper bound: 00:00 of the day after `date`. */
export function nextDayISO(date: string): string | undefined {
	if (!date) return undefined;
	const d = new Date(`${date}T00:00:00`);
	d.setDate(d.getDate() + 1);
	return d.toISOString();
}

/**
 * Format a backend ISO datetime (e.g. `2026-05-19T15:37:38`) into the
 * `DD.MM.YYYY, HH:mm` style used in the alert tables.
 */
export function formatAnalyzedAt(iso: string): string {
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return iso;
	const dd = String(d.getDate()).padStart(2, "0");
	const mm = String(d.getMonth() + 1).padStart(2, "0");
	const yyyy = d.getFullYear();
	const hh = String(d.getHours()).padStart(2, "0");
	const mi = String(d.getMinutes()).padStart(2, "0");
	return `${dd}.${mm}.${yyyy}, ${hh}:${mi}`;
}
