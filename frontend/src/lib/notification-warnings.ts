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
 * Marks a recipient entry as a reference to a named group rather than a literal
 * address. Must match `_GROUP_PREFIX` in `backend/app/notifications/config.py`,
 * which is what actually expands it at dispatch.
 */
const GROUP_PREFIX = "group:";

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

	// Recipient-bearing channel routed with nothing to deliver to: the email
	// equivalent of the webhook-with-no-URL case above, and the one that bit
	// production. The backend drops such a channel at resolve_dispatch with a
	// WARNING, so alerts stop arriving while the config still reads as
	// "Critical -> email". Observed on mainnet: 121 dropped alerts over 11 days.
	//
	// "Nothing to deliver to" is counted AFTER group expansion, matching
	// config.resolve_recipients: a `group:<alias>` entry contributes its
	// members, so a list of one alias whose group is empty (or which names no
	// existing group) resolves to zero addresses. Counting the raw list instead
	// would read `["group:soc-team"]` as covered while the backend delivers
	// nothing, reintroducing the same silent failure through the pattern the
	// Groups editor encourages.
	const expand = (recipients: string[] | undefined) =>
		(recipients ?? []).flatMap((r) =>
			r.startsWith(GROUP_PREFIX)
				? (cfg.groups?.[r.slice(GROUP_PREFIX.length)] ?? [])
				: [r],
		).length;

	for (const c of Object.keys(cfg.channels)) {
		if (c === "webhook" || !isOn(c)) continue;
		const defaultCount = expand(cfg.channels[c]?.recipients);
		if (defaultCount === 0 && routedInDefaults(c))
			out.push(
				`"${c}" is routed in the band defaults but has no recipients, so those alerts can't be delivered. Add recipients under Channels.`,
			);
		// A per-rule override REPLACES the channel default rather than merging
		// with it (triggers._resolve_recipients returns the override whenever the
		// channel key is present, including for an empty list), so a rule that
		// names the channel with an empty list delivers nothing EVEN WHEN the
		// channel default is populated. The two shapes are distinguished by key
		// presence, not by emptiness: an absent key falls back to the default.
		const deadRule = cfg.triggers.rules.some((r) => {
			if (!(r.channels ?? []).includes(c)) return false;
			const override = r.recipients?.[c];
			return override === undefined ? defaultCount === 0 : expand(override) === 0;
		});
		if (deadRule)
			out.push(
				`A per-class rule routes to "${c}" but resolves to no recipients, so it can't be delivered. A rule's recipient list replaces the Channels default rather than adding to it.`,
			);
	}

	// Enabled but never routed anywhere: the inverse mistake.
	for (const c of Object.keys(cfg.channels))
		if (isOn(c) && !routedInDefaults(c) && !routedInRules(c))
			out.push(
				`The ${c} channel is enabled but not selected in any band default or rule, so it won't fire until you route it below.`,
			);

	return out;
}
