import { expect, test as setup } from "@playwright/test";

/**
 * Redeems the bootstrap admin's magic link (minted by scripts/e2e.sh via
 * `python -m app.cli create-admin`) through the real SPA verify route, and
 * saves the resulting session as the storageState every spec reuses.
 */
setup("redeem the bootstrap admin magic link", async ({ page }) => {
	const link = process.env.E2E_ADMIN_MAGIC_LINK;
	if (!link) {
		throw new Error("E2E_ADMIN_MAGIC_LINK is not set; run the tier through scripts/e2e.sh");
	}
	await page.goto(link);
	// The verify page redeems the token client-side, then navigates.
	await expect(page.getByRole("heading", { name: "Risk Alerts" })).toBeVisible({
		timeout: 20_000,
	});
	await page.context().storageState({ path: "e2e/.auth/admin.json" });
});
