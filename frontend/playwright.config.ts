import { defineConfig, devices } from "@playwright/test";

/**
 * E2E tier: drives the real stack (built app image + Postgres + ClickHouse +
 * Mailpit) that scripts/e2e.sh brings up, seeds, and points this suite at via
 * E2E_BASE_URL. Run through that script, not bare: the suite assumes seeded
 * findings and a bootstrap admin magic link in E2E_ADMIN_MAGIC_LINK.
 *
 * The `setup` project redeems the magic link once and saves the session as
 * storageState; every spec reuses it instead of re-authenticating.
 */
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8100";

export default defineConfig({
	testDir: "./e2e",
	outputDir: "./e2e-results",
	fullyParallel: false,
	workers: 1,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI
		? [["list"], ["junit", { outputFile: "junit-e2e.xml" }]]
		: [["list"]],
	use: {
		baseURL,
		trace: "retain-on-failure",
	},
	projects: [
		{ name: "setup", testMatch: /auth\.setup\.ts/ },
		{
			name: "chromium",
			use: {
				...devices["Desktop Chrome"],
				storageState: "e2e/.auth/admin.json",
			},
			dependencies: ["setup"],
		},
	],
});
