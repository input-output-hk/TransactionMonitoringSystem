/**
 * Date / datetime helpers shared across pages.
 *
 * All formatters/parsers here treat strings as plain ISO or `yyyy-mm-dd`
 * to keep things deterministic; no locale-dependent parsing.
 */

/**
 * Parse a backend timestamp to a `Date`, or `null` if unparseable.
 *
 * Accepts both wire shapes deterministically: the canonical API form
 * (`2026-05-19T15:37:38Z`) and the legacy ClickHouse form
 * (`2026-05-19 15:37:38`, space-separated, UTC but unsuffixed). Replaces the
 * first space with `T` and appends `Z` only when no timezone is already
 * encoded, so a naive string can never be read as the client's local time.
 * This keeps the file-header "no locale-dependent parsing" promise for every
 * consumer.
 */
export function parseUtcInstant(s: string): Date | null {
	const iso = s.replace(" ", "T");
	const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso);
	const d = new Date(hasTz ? iso : `${iso}Z`);
	return Number.isNaN(d.getTime()) ? null : d;
}

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
	const d = parseUtcInstant(iso);
	if (d === null) return iso;
	const dd = String(d.getUTCDate()).padStart(2, "0");
	const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
	const yyyy = d.getUTCFullYear();
	const hh = String(d.getUTCHours()).padStart(2, "0");
	const mi = String(d.getUTCMinutes()).padStart(2, "0");
	return `${dd}.${mm}.${yyyy}, ${hh}:${mi} UTC`;
}

/**
 * Relative "time ago" formatter for live widgets. Two styles from one source:
 *
 * - default: `"17 sec"`, `"3 min"`, `"2 hr"`, `"5 days"` (dashboard tables)
 * - `{ compact: true }`: `"12s ago"`, `"5m ago"`, `"3h ago"`, `"2d ago"`
 *   (the clustering tables' denser style)
 *
 * Parsing goes through `parseUtcInstant`, so both the API's `Z`-suffixed form
 * and the legacy naive-UTC ClickHouse form are handled without a local-time
 * skew. On unparseable input the default style returns the em-dash placeholder
 * and the compact style returns the raw string (matching the prior behavior of
 * the two separate helpers this replaces).
 */
export function formatTimeAgo(
	input: string | null | undefined,
	opts?: { compact?: boolean },
): string {
	const compact = opts?.compact ?? false;
	if (!input) return compact ? "" : "—";
	const d = parseUtcInstant(input);
	if (d === null) return input;
	const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
	if (compact) {
		if (sec < 60) return `${sec}s ago`;
		if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
		if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
		return `${Math.floor(sec / 86400)}d ago`;
	}
	if (sec < 60) return `${sec} sec`;
	const min = Math.floor(sec / 60);
	if (min < 60) return `${min} min`;
	const hr = Math.floor(min / 60);
	if (hr < 24) return `${hr} hr`;
	const day = Math.floor(hr / 24);
	return `${day} day${day === 1 ? "" : "s"}`;
}
