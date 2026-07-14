/**
 * Unit tests for the notification config's silent-delivery warnings.
 * The backend accepts every one of these configs without complaint and
 * then delivers nothing, so this pre-save lint is the only guard against
 * a dead alert channel; its three warning classes are pinned here.
 */
import { describe, expect, it } from "vitest";
import type { NotificationConfig } from "@/lib/api/notifications";
import { configWarnings } from "./notification-warnings";

function baseConfig(
	overrides: Partial<NotificationConfig> = {},
): NotificationConfig {
	return {
		version: 1,
		channels: {
			email: { enabled: false, recipients: [] },
			webhook: { enabled: false, default_url: "" },
		},
		groups: {},
		triggers: { defaults: { Critical: [], High: [] }, rules: [] },
		periodic_report: {
			enabled: false,
			frequency: "weekly",
			window_days: 7,
			channels: [],
			recipients: [],
			attack_classes: "all",
			min_band: "High",
		},
		...overrides,
	};
}

describe("configWarnings", () => {
	it("passes a coherent config silently", () => {
		const cfg = baseConfig();
		cfg.channels.email.enabled = true;
		cfg.triggers.defaults.Critical = ["email"];
		expect(configWarnings(cfg)).toEqual([]);
	});

	it("warns when a band default routes a switched-off channel", () => {
		const cfg = baseConfig();
		cfg.triggers.defaults.Critical = ["email"];
		const warnings = configWarnings(cfg);
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain('"email" is routed below');
	});

	it("warns when a rule routes a switched-off channel", () => {
		const cfg = baseConfig();
		cfg.triggers.rules = [
			{ band: "High", attack_classes: ["token_dust"], channels: ["webhook"] },
		];
		const warnings = configWarnings(cfg);
		expect(warnings.some((w) => w.includes('"webhook" is routed below'))).toBe(
			true,
		);
	});

	it("dedupes the routed-but-off warning per channel", () => {
		const cfg = baseConfig();
		cfg.triggers.defaults.Critical = ["email"];
		cfg.triggers.defaults.High = ["email"];
		cfg.triggers.rules = [
			{ band: "High", attack_classes: ["phishing"], channels: ["email"] },
		];
		const offWarnings = configWarnings(cfg).filter((w) =>
			w.includes('"email"'),
		);
		expect(offWarnings).toHaveLength(1);
	});

	it("warns when webhook is routed in defaults with no default URL", () => {
		const cfg = baseConfig();
		cfg.channels.webhook.enabled = true;
		cfg.triggers.defaults.Critical = ["webhook"];
		const warnings = configWarnings(cfg);
		expect(
			warnings.some((w) => w.includes("has no Default URL")),
		).toBe(true);
	});

	it("warns when a webhook rule has no URL anywhere to fall back on", () => {
		const cfg = baseConfig();
		cfg.channels.webhook.enabled = true;
		cfg.triggers.rules = [
			{
				band: "High",
				attack_classes: ["token_dust"],
				channels: ["webhook"],
				webhook_url: "",
			},
		];
		const warnings = configWarnings(cfg);
		expect(
			warnings.some((w) => w.includes("per-class rule routes to webhook")),
		).toBe(true);
	});

	it("does not warn when the rule carries its own webhook URL", () => {
		const cfg = baseConfig();
		cfg.channels.webhook.enabled = true;
		cfg.triggers.rules = [
			{
				band: "High",
				attack_classes: ["token_dust"],
				channels: ["webhook"],
				webhook_url: "https://ops.example.com/hook",
			},
		];
		expect(
			configWarnings(cfg).filter((w) => w.toLowerCase().includes("webhook url")),
		).toEqual([]);
	});

	it("warns about an enabled channel that is never routed", () => {
		const cfg = baseConfig();
		cfg.channels.email.enabled = true;
		const warnings = configWarnings(cfg);
		expect(
			warnings.some((w) =>
				w.includes("enabled but not selected in any band default or rule"),
			),
		).toBe(true);
	});

	it("whitespace-only default URL counts as missing", () => {
		const cfg = baseConfig();
		cfg.channels.webhook.enabled = true;
		cfg.channels.webhook.default_url = "   ";
		cfg.triggers.defaults.Critical = ["webhook"];
		expect(
			configWarnings(cfg).some((w) => w.includes("has no Default URL")),
		).toBe(true);
	});
});
