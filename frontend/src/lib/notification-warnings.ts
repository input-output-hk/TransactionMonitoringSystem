/**
 * Pre-save lint for the notification config.
 *
 * Lives outside the settings page so it is unit-testable (a component
 * file may only export components for fast refresh) and reusable.
 */
import type { NotificationConfig } from "@/lib/api/notifications";

/** Risk bands in severity order, as the trigger matrix displays them. */
export const BANDS = ["Critical", "High", "Moderate", "Informational"] as const;

/**
 * Human-readable config problems that silently prevent delivery, surfaced
 * before save so an operator doesn't repeat the classic mistakes: routing a
 * channel that is switched off, or routing webhook with nowhere to POST. A
 * disabled channel is skipped at dispatch no matter what the matrix or rules
 * say, so the backend accepts these without complaint and nothing arrives.
 */
export function configWarnings(cfg: NotificationConfig): string[] {
	const out: string[] = [];
	const isOn = (c: string) => !!cfg.channels[c]?.enabled;
	const webhookUrl = cfg.channels.webhook?.default_url?.trim();
	const routedInDefaults = (c: string) =>
		BANDS.some((b) => (cfg.triggers.defaults[b] ?? []).includes(c));
	const routedInRules = (c: string) =>
		cfg.triggers.rules.some((r) => (r.channels ?? []).includes(c));

	// Routed (matrix or rule) but the channel itself is switched off: the
	// master-gate mistake. One line per channel, whichever way it's routed.
	const offButRouted = new Set<string>();
	for (const band of BANDS)
		for (const c of cfg.triggers.defaults[band] ?? [])
			if (!isOn(c)) offButRouted.add(c);
	for (const r of cfg.triggers.rules)
		for (const c of r.channels ?? []) if (!isOn(c)) offButRouted.add(c);
	for (const c of offButRouted)
		out.push(
			`"${c}" is routed below but the ${c} channel is off under Channels, so it never fires. Enable it above.`,
		);

	// Webhook routed but with nowhere to deliver.
	if (isOn("webhook") && !webhookUrl && routedInDefaults("webhook"))
		out.push(
			"Webhook is routed in the band defaults but has no Default URL, so those alerts can't be delivered. Set a Default URL under Channels.",
		);
	if (
		isOn("webhook") &&
		!webhookUrl &&
		cfg.triggers.rules.some(
			(r) => (r.channels ?? []).includes("webhook") && !r.webhook_url?.trim(),
		)
	)
		out.push(
			"A per-class rule routes to webhook but sets no URL, and Channels has no Default URL to fall back on, so it can't be delivered.",
		);

	// Enabled but never routed anywhere: the inverse mistake.
	for (const c of Object.keys(cfg.channels))
		if (isOn(c) && !routedInDefaults(c) && !routedInRules(c))
			out.push(
				`The ${c} channel is enabled but not selected in any band default or rule, so it won't fire until you route it below.`,
			);

	return out;
}
