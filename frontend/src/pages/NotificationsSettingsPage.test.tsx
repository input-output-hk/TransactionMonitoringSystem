/**
 * Smoke tests for the notifications settings page. The page is admin-only
 * glue with no unit-testable logic of its own, so these pin the structural
 * contract of the Channels master section: the page renders from a fetched
 * config, each channel's switch exposes the stored enabled state, and
 * toggling a switch is mirrored by the header state chips.
 */
import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NotificationConfigResponse } from "@/lib/api/notifications";
import { NotificationsSettingsPage } from "./NotificationsSettingsPage";

vi.mock("@/lib/api/notifications", async (importOriginal) => ({
	...(await importOriginal<typeof import("@/lib/api/notifications")>()),
	fetchNotificationConfig: vi.fn(() => Promise.resolve(RESPONSE)),
}));

const RESPONSE: NotificationConfigResponse = {
	config: {
		version: 1,
		channels: {
			email: { enabled: true, recipients: ["ops@example.com"] },
			webhook: { enabled: false, default_url: "" },
		},
		groups: {},
		triggers: { defaults: { High: ["email"] }, rules: [] },
		periodic_report: {
			enabled: false,
			frequency: "weekly",
			window_days: 7,
			channels: ["email"],
			recipients: [],
			attack_classes: "all",
			min_band: "Moderate",
		},
	},
	secrets_status: {
		smtp_configured: true,
		webhook_signing_secret_configured: false,
	},
	clustering_enabled: false,
};

function renderPage() {
	const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	return render(
		<QueryClientProvider client={qc}>
			<NotificationsSettingsPage />
		</QueryClientProvider>,
	);
}

afterEach(cleanup);

describe("NotificationsSettingsPage channels section", () => {
	it("renders the channel master switches with the stored enabled state", async () => {
		renderPage();
		const email = await screen.findByRole("switch", {
			name: "Email channel enabled",
		});
		const webhook = screen.getByRole("switch", {
			name: "Webhook channel enabled",
		});
		expect(email).toBeChecked();
		expect(webhook).not.toBeChecked();
		// The header chips read from the same editable state as the switches.
		expect(screen.getByText("email on")).toBeInTheDocument();
		expect(screen.getByText("webhook off")).toBeInTheDocument();
	});

	it("mirrors a toggled master switch in its header chip", async () => {
		renderPage();
		const webhook = await screen.findByRole("switch", {
			name: "Webhook channel enabled",
		});
		fireEvent.click(webhook);
		expect(webhook).toBeChecked();
		expect(screen.getByText("webhook on")).toBeInTheDocument();
		expect(screen.queryByText("webhook off")).not.toBeInTheDocument();
	});
});
