/**
 * Notification-config admin API client. Talks to `/api/notifications/config`
 * (admin-only). The config is a single structured document round-tripped
 * verbatim — no snake/camel mapping, so what we PUT is exactly what the
 * backend validator expects.
 */
import { fetchWithAuth } from "./fetch";

export type ChannelConfig = {
	enabled: boolean;
	recipients?: string[]; // email
	default_url?: string; // webhook
};

export type TriggerRule = {
	band: string;
	attack_classes: string[];
	channels: string[];
	recipients?: Record<string, string[]>; // per-channel override
	webhook_url?: string;
};

export type PeriodicReportConfig = {
	enabled: boolean;
	frequency: "daily" | "weekly" | "monthly";
	window_days: number;
	channels: string[];
	recipients: string[];
	attack_classes: "all" | string[];
	min_band: string;
};

export type NotificationConfig = {
	version: number;
	channels: Record<string, ChannelConfig>;
	groups: Record<string, string[]>;
	triggers: {
		defaults: Record<string, string[]>;
		rules: TriggerRule[];
	};
	periodic_report: PeriodicReportConfig;
};

export type SecretsStatus = {
	webhook_signing_secret_configured: boolean;
	smtp_configured: boolean;
};

export type NotificationConfigResponse = {
	config: NotificationConfig;
	secrets_status: SecretsStatus;
};

export async function fetchNotificationConfig(): Promise<NotificationConfigResponse> {
	const res = await fetchWithAuth("/api/notifications/config");
	if (!res.ok) {
		throw new Error(`load notification config failed (${res.status})`);
	}
	return (await res.json()) as NotificationConfigResponse;
}

export async function updateNotificationConfig(
	config: NotificationConfig,
): Promise<void> {
	const res = await fetchWithAuth("/api/notifications/config", {
		method: "PUT",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(config),
	});
	if (!res.ok) {
		// The backend returns the precise validation message in `detail` (422).
		const err = (await res.json().catch(() => null)) as
			| { detail?: string }
			| null;
		throw new Error(err?.detail ?? `save failed (${res.status})`);
	}
}
