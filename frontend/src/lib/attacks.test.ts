/**
 * Risk-band helpers and the Avg Risk explanatory copy.
 *
 * The band thresholds are duplicated from the backend's normalise module (no
 * endpoint exposes them), so these tests pin the mapping the UI shows against
 * the boundaries that module defines.
 */
import { describe, expect, it } from "vitest";

import {
	avgRiskHelp,
	SCORE_MAX,
	SEVERITY_INFORMATIONAL_MAX,
	SEVERITY_MIN_SCORE,
	severityForScore,
	type Severity,
} from "@/lib/attacks";

describe("severityForScore", () => {
	it("maps each band's lower boundary to that band", () => {
		expect(severityForScore(SEVERITY_MIN_SCORE.CRITICAL)).toBe("CRITICAL");
		expect(severityForScore(SEVERITY_MIN_SCORE.HIGH)).toBe("HIGH");
		expect(severityForScore(SEVERITY_MIN_SCORE.MODERATE)).toBe("MODERATE");
		expect(severityForScore(0)).toBe("INFORMATIONAL");
	});

	it("maps each band's upper boundary to that band", () => {
		expect(severityForScore(SEVERITY_MIN_SCORE.CRITICAL - 1)).toBe("HIGH");
		expect(severityForScore(SEVERITY_MIN_SCORE.HIGH - 1)).toBe("MODERATE");
		expect(severityForScore(SEVERITY_MIN_SCORE.MODERATE - 1)).toBe(
			"INFORMATIONAL",
		);
	});

	it("does not leave a dead zone between Informational and Moderate", () => {
		// Scores are rounded to 2dp, so a value between the documented bands must
		// not silently under-band toward Informational (which would be
		// recall-negative). Mirrors the backend's `> BAND_INFORMATIONAL_MAX` test.
		expect(severityForScore(30.5)).toBe("MODERATE");
	});

	it("handles a full-scale score", () => {
		expect(severityForScore(100)).toBe("CRITICAL");
	});
});

describe("avgRiskHelp", () => {
	it("states the scale and the boundaries the banding function applies", () => {
		const text = avgRiskHelp(50);
		expect(text).toContain(`0-${SCORE_MAX}`);
		expect(text).toContain(`up to ${SEVERITY_INFORMATIONAL_MAX}`);
		expect(text).toContain(`above ${SEVERITY_INFORMATIONAL_MAX}`);
		expect(text).toContain(`from ${SEVERITY_MIN_SCORE.HIGH}`);
		expect(text).toContain(`from ${SEVERITY_MIN_SCORE.CRITICAL}`);
	});

	it("never contradicts itself on a fractional score", () => {
		// The client asked for a helper saying whether the number is high or low,
		// so the boundaries it quotes have to agree with the band it names. Stated
		// as whole-number ranges they did not: 30.5 bands as MODERATE while
		// "Informational under 31" in the same tooltip called it Informational.
		const text = avgRiskHelp(30.5);
		expect(severityForScore(30.5)).toBe("MODERATE");
		expect(text).toContain("Moderate band");
		expect(text).not.toContain(`under ${SEVERITY_MIN_SCORE.MODERATE}`);
	});

	it("says which band the current value is in, so 'high or low' is answerable", () => {
		// The client's complaint was that the number's meaning was unclear, which
		// includes not knowing whether a given value is good or bad.
		expect(avgRiskHelp(95)).toContain("Critical band");
		expect(avgRiskHelp(65)).toContain("High band");
		expect(avgRiskHelp(40)).toContain("Moderate band");
		expect(avgRiskHelp(5)).toContain("Informational band");
	});

	it("omits the band sentence when there is no value yet", () => {
		for (const empty of [null, undefined]) {
			const text = avgRiskHelp(empty);
			expect(text).not.toContain("current average");
			// The scale explanation still renders, so the tooltip is never blank.
			expect(text).toContain("0-100");
		}
	});

	it("states that it excludes clean transactions", () => {
		// This is the substance of the fix: the aggregate used to average every
		// scored transaction, including the score-0 ones the table never lists.
		expect(avgRiskHelp(50)).toContain("clean");
	});

	it("states that it ignores the table's filters", () => {
		// Network-wide by design, so it will not move with the filters beside it.
		expect(avgRiskHelp(50)).toContain("filters");
	});

	it("covers every severity without throwing", () => {
		const severities: Severity[] = [
			"INFORMATIONAL",
			"MODERATE",
			"HIGH",
			"CRITICAL",
		];
		for (const s of severities) {
			expect(avgRiskHelp(SEVERITY_MIN_SCORE[s])).toBeTruthy();
		}
	});
});
