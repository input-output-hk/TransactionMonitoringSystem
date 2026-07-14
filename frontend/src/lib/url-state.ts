/**
 * Shared helpers for pages that keep filter/tab/pagination state in the URL
 * (ReportsPage filters, ValidatorDetailPage tab). One home for the subtle
 * rules so they cannot drift between pages: validate-or-default reads,
 * replace-history writes (Back must not step through every filter change),
 * null deletes a param (defaults stay out of shared links), and a filter
 * write can atomically drop the sibling page offset it invalidates.
 */
import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

/** Plain calendar date (YYYY-MM-DD); anything else falls back. */
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Read a param constrained to an allowed set, else the fallback. */
export function qpEnum<T extends string>(
	params: URLSearchParams,
	key: string,
	allowed: readonly T[],
	fallback: T,
): T {
	const v = params.get(key);
	return v !== null && (allowed as readonly string[]).includes(v)
		? (v as T)
		: fallback;
}

/** Read a YYYY-MM-DD date param, else the fallback. */
export function qpDate(
	params: URLSearchParams,
	key: string,
	fallback: string,
): string {
	const v = params.get(key);
	return v && ISO_DATE_RE.test(v) ? v : fallback;
}

export type SetParamOptions = {
	/**
	 * Sibling params to drop alongside this write. The canonical use is a
	 * page offset: after a filter change the old page is meaningless
	 * against the new result set.
	 */
	alsoDelete?: readonly string[];
};

/**
 * useSearchParams wrapped in the shared write discipline. `value: null`
 * deletes the key; writes replace history instead of pushing.
 */
export function useQueryParamState() {
	const [searchParams, setSearchParams] = useSearchParams();
	const setParam = useCallback(
		(key: string, value: string | null, opts?: SetParamOptions) => {
			setSearchParams(
				(prev) => {
					const next = new URLSearchParams(prev);
					if (value === null) {
						next.delete(key);
					} else {
						next.set(key, value);
					}
					for (const k of opts?.alsoDelete ?? []) {
						next.delete(k);
					}
					return next;
				},
				{ replace: true },
			);
		},
		[setSearchParams],
	);
	return { searchParams, setParam };
}
