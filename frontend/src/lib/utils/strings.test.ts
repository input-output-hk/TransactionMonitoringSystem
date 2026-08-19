/**
 * The project-wide truncation helper.
 *
 * `shortHash` is the only place a hash, address or asset unit gets shortened, so
 * a wrong result here is wrong everywhere at once. The zero-tail case is the one
 * that bit: callers pass `tail = 0` to ask for head-only truncation (an asset
 * name reads head-first and has no meaningful suffix), and `slice(-0)` is
 * `slice(0)`, i.e. the whole string.
 */
import { describe, expect, it } from "vitest";

import { shortHash } from "@/lib/utils/strings";

const HEX = "4361726461536e656b4e4654313233"; // 30 chars, a plausible asset name

describe("shortHash", () => {
	it("middle-truncates with both a head and a tail", () => {
		expect(shortHash(HEX, 8, 4)).toBe("43617264...3233");
	});

	it("returns the input untouched when truncating would not shorten it", () => {
		// head + tail + 3 is the break-even point: at or below it the "..." glyph
		// costs more than it saves.
		const short = "abcdef";
		expect(shortHash(short, 4, 4)).toBe(short);
	});

	describe("a zero tail means head-only", () => {
		it("does not echo the whole string back after the ellipsis", () => {
			expect(shortHash(HEX, 10, 0)).toBe("4361726461...");
		});

		it("is shorter than the input, which is the point of truncating", () => {
			// The bug produced head + "..." + input: 43 chars from a 30-char input,
			// rendering as two values run together.
			expect(shortHash(HEX, 10, 0).length).toBeLessThan(HEX.length);
			expect(shortHash(HEX, 10, 0)).not.toContain(HEX);
		});

		it("still leaves a short input alone", () => {
			expect(shortHash("abcdefgh", 10, 0)).toBe("abcdefgh");
		});
	});
});
