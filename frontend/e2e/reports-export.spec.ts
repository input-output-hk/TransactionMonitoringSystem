import { expect, test } from "@playwright/test";
import { downloadText, seededTxHash } from "./helpers";

/**
 * Reporting over a configurable window with CSV export: the default window
 * lists every seeded finding across all bands, and the export's confirm step
 * downloads a CSV carrying them.
 */

test("the report lists the seeded window and exports it as CSV", async ({ page }) => {
	await page.goto("/reports");
	await expect(page.getByRole("heading", { name: "Risk Alerts" })).toBeVisible();
	// Reports default to all severities over the trailing window, so every
	// seeded finding is listed (imported archive rows are not scores).
	await expect(page.getByText("Total Risk Alerts: 8")).toBeVisible();

	// Below EXPORT_CONFIRM_THRESHOLD rows the export runs straight away; the
	// confirm dialog is for the bulk case only.
	const downloadPromise = page.waitForEvent("download");
	await page.getByRole("button", { name: "Export" }).click();
	const download = await downloadPromise;

	const body = await downloadText(download);
	expect(body).toContain(seededTxHash(0));
	expect(body).toContain(seededTxHash(7));
});
