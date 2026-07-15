import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	defaultEnd,
	defaultStart,
	formatAnalyzedAt,
	formatTimeAgo,
	nDaysAgoISODate,
	nextDayISO,
	startOfDayISO,
	todayISODate,
} from "./dates";

// Fixed instant so day-boundary math is deterministic regardless of the
// machine's clock or timezone.
const NOW = new Date("2026-07-06T12:00:00Z");

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOW);
});

afterEach(() => {
	vi.useRealTimers();
});

describe("calendar-date helpers", () => {
	it("todayISODate returns the UTC calendar date", () => {
		expect(todayISODate()).toBe("2026-07-06");
	});

	it("nDaysAgoISODate counts back across month boundaries", () => {
		expect(nDaysAgoISODate(6)).toBe("2026-06-30");
	});

	it("default Reports range is 30 days back to today", () => {
		expect(defaultStart()).toBe("2026-06-06");
		expect(defaultEnd()).toBe("2026-07-06");
	});
});

describe("range boundaries", () => {
	it("startOfDayISO anchors at UTC midnight, not local", () => {
		expect(startOfDayISO("2026-05-19")).toBe("2026-05-19T00:00:00.000Z");
	});

	it("nextDayISO is the exclusive upper bound", () => {
		expect(nextDayISO("2026-05-19")).toBe("2026-05-20T00:00:00.000Z");
	});

	it("crosses month and year ends correctly", () => {
		expect(nextDayISO("2026-12-31")).toBe("2027-01-01T00:00:00.000Z");
	});

	it("empty input is undefined, not an Invalid Date", () => {
		expect(startOfDayISO("")).toBeUndefined();
		expect(nextDayISO("")).toBeUndefined();
	});
});

describe("formatAnalyzedAt", () => {
	it("treats a naive ClickHouse datetime as UTC and labels it", () => {
		// No timezone suffix: must NOT be reinterpreted as local time.
		expect(formatAnalyzedAt("2026-05-19T15:37:38")).toBe(
			"19.05.2026, 15:37 UTC",
		);
	});

	it("respects an explicit offset", () => {
		expect(formatAnalyzedAt("2026-05-19T15:37:38+02:00")).toBe(
			"19.05.2026, 13:37 UTC",
		);
	});

	it("passes through unparseable and empty input unchanged", () => {
		expect(formatAnalyzedAt("not-a-date")).toBe("not-a-date");
		expect(formatAnalyzedAt("")).toBe("");
	});
});

describe("formatTimeAgo", () => {
	it("formats each magnitude compactly", () => {
		expect(formatTimeAgo("2026-07-06T11:59:43Z")).toBe("17 sec");
		expect(formatTimeAgo("2026-07-06T11:57:00Z")).toBe("3 min");
		expect(formatTimeAgo("2026-07-06T10:00:00Z")).toBe("2 hr");
		expect(formatTimeAgo("2026-07-01T12:00:00Z")).toBe("5 days");
		expect(formatTimeAgo("2026-07-05T12:00:00Z")).toBe("1 day");
	});

	it("treats a naive datetime as UTC (no local-offset skew)", () => {
		expect(formatTimeAgo("2026-07-06T11:59:00")).toBe("1 min");
	});

	it("clamps future timestamps to zero instead of going negative", () => {
		expect(formatTimeAgo("2026-07-06T12:00:30Z")).toBe("0 sec");
	});

	it("renders a placeholder for missing values", () => {
		expect(formatTimeAgo(null)).toBe("—");
		expect(formatTimeAgo(undefined)).toBe("—");
	});
});

describe("formatTimeAgo (compact)", () => {
	const compact = { compact: true } as const;

	it("formats each magnitude in the dense style", () => {
		expect(formatTimeAgo("2026-07-06T11:59:43Z", compact)).toBe("17s ago");
		expect(formatTimeAgo("2026-07-06T11:57:00Z", compact)).toBe("3m ago");
		expect(formatTimeAgo("2026-07-06T10:00:00Z", compact)).toBe("2h ago");
		expect(formatTimeAgo("2026-07-01T12:00:00Z", compact)).toBe("5d ago");
	});

	it("parses the legacy space-separated ClickHouse UTC form", () => {
		expect(formatTimeAgo("2026-07-06 11:57:00", compact)).toBe("3m ago");
	});

	it("parses an explicit offset without a local-time skew", () => {
		expect(formatTimeAgo("2026-07-06T13:57:00+02:00", compact)).toBe("3m ago");
	});

	it("returns the raw string when unparseable", () => {
		expect(formatTimeAgo("not-a-date", compact)).toBe("not-a-date");
	});
});
