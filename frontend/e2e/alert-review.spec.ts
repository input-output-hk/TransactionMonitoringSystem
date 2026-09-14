import { expect, test } from "@playwright/test";
import { seededTxHash, shortHash } from "./helpers";

/**
 * The alert-review workflow over seeded findings: the default High + Critical
 * view shows exactly the seeded alerting rows, and opening one exposes the
 * full risk score with its sub-scores.
 */

test("seeded detections render in the default High + Critical view", async ({ page }) => {
	await page.goto("/dashboard");
	// The seed writes 3 Critical + 3 High (the 2 Moderate rows are outside the
	// default severity filter).
	await expect(page.getByText("Total Risk Alerts: 6")).toBeVisible();
	await expect(page.getByText(shortHash(seededTxHash(0))).first()).toBeVisible();
	await expect(page.getByText("CRITICAL").first()).toBeVisible();
});

test("widening the severity filter reveals the Moderate findings", async ({ page }) => {
	// Filters live in the URL, so drive them there (the UI multi-select writes
	// the same params).
	await page.goto("/dashboard?severity=INFORMATIONAL,MODERATE,HIGH,CRITICAL");
	await expect(page.getByText("Total Risk Alerts: 8")).toBeVisible();
});

test("an alert opens to its risk score and sub-scores", async ({ page }) => {
	// Seeded finding 0: large_datum, Critical, score 90.
	await page.goto(`/attacks/${seededTxHash(0)}`);
	await expect(page.getByRole("heading", { name: "Attack Detail" })).toBeVisible();
	await expect(page.getByText("RISK SCORE")).toBeVisible();
	await expect(page.getByText("90/100")).toBeVisible();
	await expect(page.getByText("Sub-scores")).toBeVisible();
});
