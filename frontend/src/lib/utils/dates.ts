/**
 * Date / datetime helpers shared across pages.
 *
 * All formatters/parsers here treat strings as plain ISO or `yyyy-mm-dd`
 * to keep things deterministic; no locale-dependent parsing.
 */

/** Today's date in `yyyy-mm-dd` form (UTC, matching the UTC display + range
 *  boundaries below so the Reports range is internally consistent). */
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

/** Convert a `yyyy-mm-dd` date to an ISO datetime at start of day (UTC).
 *  UTC (the trailing Z) so the range boundary matches the UTC calendar date
 *  the pickers show; parsing as local midnight shifted the boundary by the
 *  client's offset for non-UTC operators. */
export function startOfDayISO(date: string): string | undefined {
	if (!date) return undefined;
	return new Date(`${date}T00:00:00Z`).toISOString();
}

/** Exclusive upper bound: 00:00 UTC of the day after `date`. */
export function nextDayISO(date: string): string | undefined {
	if (!date) return undefined;
	const d = new Date(`${date}T00:00:00Z`);
	d.setUTCDate(d.getUTCDate() + 1);
	return d.toISOString();
}

/**
 * Format a backend ISO datetime (e.g. `2026-05-19T15:37:38`) into the
 * `DD.MM.YYYY, HH:mm UTC` style used in the alert tables.
 *
 * Backend datetimes from ClickHouse arrive as naive ISO (no `Z`) but are UTC.
 * Plain `new Date("...")` parsed them as LOCAL, so the columns showed UTC
 * wall-clock digits reinterpreted as local and unlabeled -- disagreeing with
 * the relative "time ago" widget (which normalizes) by the client's offset,
 * and misleading operators who correlate against UTC logs. Normalize the naive
 * string to UTC, render with UTC getters, and label it `UTC`.
 */
export function formatAnalyzedAt(iso: string): string {
	if (!iso) return iso;
	const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso);
	const d = new Date(hasTz ? iso : `${iso}Z`);
	if (Number.isNaN(d.getTime())) return iso;
	const dd = String(d.getUTCDate()).padStart(2, "0");
	const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
	const yyyy = d.getUTCFullYear();
	const hh = String(d.getUTCHours()).padStart(2, "0");
	const mi = String(d.getUTCMinutes()).padStart(2, "0");
	return `${dd}.${mm}.${yyyy}, ${hh}:${mi} UTC`;
}

/**
 * Compact "time ago" formatter for live widgets — e.g. "17 sec", "3 min",
 * "2 hr", "5 days".
 *
 * Backend datetimes from ClickHouse arrive as naive ISO (no `Z` suffix)
 * but represent UTC. Plain `new Date("2026-05-22T08:00:18")` would parse
 * those as LOCAL time, which throws the elapsed calculation off by the
 * client's UTC offset. We append `Z` when the timezone isn't already
 * encoded to keep parsing deterministic.
 */
export function formatTimeAgo(iso: string | null | undefined): string {
	if (!iso) return "—";
	const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso);
	const d = new Date(hasTz ? iso : `${iso}Z`);
	if (Number.isNaN(d.getTime())) return iso;
	const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
	if (sec < 60) return `${sec} sec`;
	const min = Math.floor(sec / 60);
	if (min < 60) return `${min} min`;
	const hr = Math.floor(min / 60);
	if (hr < 24) return `${hr} hr`;
	const day = Math.floor(hr / 24);
	return `${day} day${day === 1 ? "" : "s"}`;
}
