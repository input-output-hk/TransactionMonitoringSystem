import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatAge } from "./format";

describe("formatAge", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-07-15T12:00:00Z"));
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it("parses legacy ClickHouse space-separated UTC strings", () => {
		expect(formatAge("2026-07-15 11:55:00")).toBe("5m ago");
	});

	it("parses the canonical Z-suffixed ISO form without double-suffixing", () => {
		expect(formatAge("2026-07-15T11:55:00Z")).toBe("5m ago");
	});

	it("parses explicit-offset timestamps", () => {
		expect(formatAge("2026-07-15T13:55:00+02:00")).toBe("5m ago");
	});

	it("returns the raw string when unparseable", () => {
		expect(formatAge("not-a-date")).toBe("not-a-date");
	});
});
