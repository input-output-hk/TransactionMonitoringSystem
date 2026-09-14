import { expect, test } from "@playwright/test";
import { seededTxHash, shortHash } from "./helpers";

/**
 * The false-positive archive round-trip: archiving demands a reason, the
 * archived alert lands in the Archive with that reason, and restore returns
 * it to the active view. Leaves the data exactly as seeded.
 */

test("archive with a required reason, then restore", async ({ page }) => {
	// Seeded finding 1: large_datum, Critical, score 86.
	const hash = seededTxHash(1);
	await page.goto(`/attacks/${hash}`);
	await expect(page.getByRole("heading", { name: "Attack Detail" })).toBeVisible();

	await page.locator('button[title="Delete"]').click();
	await expect(page.getByText("Are you sure this is not an attack?")).toBeVisible();

	// Confirm stays disabled until a reason is chosen: the guard the review
	// verified at the UI.
	const confirm = page.getByRole("button", { name: "Confirm" });
	await expect(confirm).toBeDisabled();
	await page.locator("#archive-reason").click();
	await page.getByRole("option", { name: "False positive" }).click();
	await page.locator("#archive-notes").fill("e2e archive round-trip");
	await confirm.click();

	// Archiving navigates to the Archive, where the row carries its reason.
	await expect(page).toHaveURL(/\/archive/);
	await expect(page.getByRole("heading", { name: "Archived Attacks" })).toBeVisible();
	const row = page.getByText(shortHash(hash));
	await expect(row).toBeVisible();
	await expect(page.getByText("False positive: e2e archive round-trip")).toBeVisible();

	// Restore returns it to the dashboard's active set.
	await row.click();
	await expect(page.getByRole("heading", { name: "Archived Attack Detail" })).toBeVisible();
	await page.getByRole("button", { name: "Restore" }).click();
	await page.getByRole("button", { name: "Confirm" }).click();
	await expect(page).toHaveURL(/\/dashboard/);

	await page.goto(`/attacks/${hash}`);
	await expect(page.getByRole("heading", { name: "Attack Detail", exact: true })).toBeVisible();
});
