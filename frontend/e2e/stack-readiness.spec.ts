import { expect, test } from "@playwright/test";

/**
 * Whole-stack readiness: the built image serves a healthy API and a working
 * SPA, unauthenticated visitors land on sign-in, and the authenticated
 * dashboard shell renders its panels without console errors. This is the
 * "tests pass but the stack does not come up" gap the external review named.
 */

test("the API reports healthy", async ({ request }) => {
	const res = await request.get("/health");
	expect(res.status()).toBe(200);
	expect(await res.json()).toMatchObject({ status: "healthy" });
});

test.describe("unauthenticated", () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test("a visitor without a session lands on sign-in", async ({ page }) => {
		await page.goto("/");
		await expect(page).toHaveURL(/\/login$/);
		await expect(page.getByRole("heading", { name: "TMS" })).toBeVisible();
		await expect(page.getByRole("button", { name: "Send Magic Link" })).toBeVisible();
	});
});

test("the dashboard shell renders its panels without console errors", async ({ page }) => {
	const consoleErrors: string[] = [];
	page.on("console", (msg) => {
		if (msg.type() === "error") consoleErrors.push(msg.text());
	});
	await page.goto("/dashboard");
	await expect(page.getByRole("heading", { name: "Risk Alerts" })).toBeVisible();
	await expect(page.getByText("TX / min")).toBeVisible();
	await expect(page.getByText("Latest Transactions")).toBeVisible();
	await expect(page.getByText("Latest Blocks")).toBeVisible();
	expect(consoleErrors).toEqual([]);
});
