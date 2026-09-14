import { type APIRequestContext, expect, test } from "@playwright/test";
import { MAILPIT_URL } from "./helpers";

/**
 * Role-based access, exercised the way a real deployment onboards a reviewer:
 * the admin invites through the UI, Mailpit captures the invite, the reviewer
 * redeems the magic link in a fresh browser context, and both the UI and the
 * API deny user management: the 403 failure path the external review asked to
 * see covered.
 */

// Unique per run: the invite is a real user row, so a fixed address would
// collide (409) against a stack kept alive with E2E_KEEP_STACK=1.
const REVIEWER_EMAIL = `reviewer-e2e-${Date.now()}@example.com`;

async function fetchInviteToken(request: APIRequestContext, email: string): Promise<string> {
	const deadline = Date.now() + 20_000;
	while (Date.now() < deadline) {
		const search = await request.get(
			`${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:${email}`)}`,
		);
		if (search.ok()) {
			const { messages } = (await search.json()) as { messages?: Array<{ ID: string }> };
			if (messages?.length) {
				const msg = await request.get(`${MAILPIT_URL}/api/v1/message/${messages[0].ID}`);
				if (msg.ok()) {
					const { Text } = (await msg.json()) as { Text?: string };
					const m = Text?.match(/\/auth\/verify\?token=([A-Za-z0-9._~-]+)/);
					if (m) return m[1];
				}
			}
		}
		await new Promise((r) => setTimeout(r, 500));
	}
	throw new Error(`no invite email for ${email} reached Mailpit within 20s`);
}

test("an invited reviewer is denied user management in the UI and with 403 at the API", async ({
	page,
	browser,
	request,
}) => {
	// Positive control first: the admin session reads the user list.
	const adminList = await page.request.get("/api/v1/users");
	expect(adminList.status()).toBe(200);

	// Invite the reviewer through the real admin UI (role defaults to Reviewer).
	await page.goto("/users");
	await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
	await page.getByRole("button", { name: "Add User" }).click();
	await page.locator("#add-fullname").fill("E2E Reviewer");
	await page.locator("#add-email").fill(REVIEWER_EMAIL);
	await page.getByRole("button", { name: "Confirm" }).click();
	await expect(
		page.getByText(`An invitation email has been sent to ${REVIEWER_EMAIL}`),
	).toBeVisible();

	// Fish the magic link out of Mailpit and redeem it in a fresh,
	// unauthenticated context: the reviewer's browser.
	const token = await fetchInviteToken(request, REVIEWER_EMAIL);
	// An explicitly empty storageState: a context created here inherits the
	// project's `use` options, and inheriting the admin's saved session would
	// make the reviewer browse as the admin and quietly pass this test.
	const reviewer = await browser.newContext({ storageState: { cookies: [], origins: [] } });
	try {
		const rpage = await reviewer.newPage();
		await rpage.goto(`/auth/verify?token=${token}`);
		await expect(rpage.getByRole("heading", { name: "Risk Alerts" })).toBeVisible({
			timeout: 20_000,
		});

		// UI: no Users nav item, and /users bounces back to the dashboard.
		await expect(rpage.getByRole("link", { name: "Users" })).toHaveCount(0);
		await rpage.goto("/users");
		await expect(rpage).toHaveURL(/\/dashboard/);

		// API: the role gate answers 403 on the admin-only endpoint, with the
		// reviewer's real session cookie attached: enforced server-side, not
		// just hidden in the UI.
		const denied = await rpage.request.get("/api/v1/users");
		expect(denied.status()).toBe(403);
	} finally {
		await reviewer.close();
	}
});
