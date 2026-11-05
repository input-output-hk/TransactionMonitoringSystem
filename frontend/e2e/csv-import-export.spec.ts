import { expect, test } from "@playwright/test";
import { downloadText, importTxHash, shortHash } from "./helpers";

/**
 * The CSV workflows: import externally sourced attack data with per-row
 * validation and an explicit confirm, then export the archive and check the
 * rows come back out.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

/** An archived_at the given number of days before now. The Archive opens on a
 *  trailing date range, so a fixed date would age out of it and fail later. */
function daysAgo(days: number): string {
	return new Date(Date.now() - days * DAY_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
}

test("import a CSV of external attack data, then export the archive", async ({ page }) => {
	const [h0, h1] = [importTxHash(0), importTxHash(1)];
	const csv = [
		"network,tx_hash,note,archived_by,archived_at,source",
		`preprod,${h0},False positive: e2e import,e2e@example.com,${daysAgo(2)},local`,
		`preprod,${h1},Authorized test,e2e@example.com,${daysAgo(1)},local`,
		"", // trailing newline
	].join("\n");

	await page.goto("/import");
	await expect(page.getByText("Select a file or drag and drop here")).toBeVisible();
	await page.locator('input[type="file"]').setInputFiles({
		name: "e2e-import.csv",
		mimeType: "text/csv",
		buffer: Buffer.from(csv),
	});
	await expect(page.getByText("2 valid")).toBeVisible();

	await page.getByRole("button", { name: "Upload" }).click();
	await expect(page.getByText("Import 2 non-attacks?")).toBeVisible();
	await page.getByRole("button", { name: "Confirm" }).click();

	// Success lands on the Archive with both imported rows present.
	await expect(page).toHaveURL(/\/archive/);
	await expect(page.getByText(shortHash(h0))).toBeVisible();
	await expect(page.getByText(shortHash(h1))).toBeVisible();

	// Export the archive and check the imported rows survive the round trip.
	const downloadPromise = page.waitForEvent("download");
	await page.getByRole("button", { name: "Export" }).click();
	const download = await downloadPromise;
	const body = await downloadText(download);
	expect(body).toContain(h0);
	expect(body).toContain(h1);
});
