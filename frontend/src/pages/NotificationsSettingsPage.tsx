/**
 * Admin-only page to manage the notification config (channels, groups, the
 * band×attack-class trigger matrix + per-rule overrides, and periodic-report
 * settings) — the runtime replacement for the former notifications.yaml.
 * Loads from / saves to `/api/notifications/config`; a save takes effect with
 * no restart. Secrets (SMTP, webhook signing key) are env-managed and shown
 * read-only here.
 */
import { SUPERVISED_ATTACK_CLASS_OPTIONS } from "@/lib/api/analysis";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import {
	type ChannelConfig,
	type NotificationConfig,
	type PeriodicReportConfig,
	type TriggerRule,
	fetchNotificationConfig,
	updateNotificationConfig,
} from "@/lib/api/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { BANDS, configWarnings } from "@/lib/notification-warnings";

const QK = ["notifications", "config"] as const;
// "Moderate" is the canonical band (backend enum + stored config keys); the UI
// shows it as "Medium" to match the dashboard. Values stay canonical on save.
const BAND_LABEL: Record<string, string> = { Moderate: "Medium" };
const bandLabel = (b: string) => BAND_LABEL[b] ?? b;
const FREQUENCIES = ["daily", "weekly", "monthly"] as const;
// Single-sourced from the canonical attack-class map (analysis.ts) so this
// picker cannot drift from the classes the engine actually scores.
const ATTACK_CLASS_OPTIONS = SUPERVISED_ATTACK_CLASS_OPTIONS;
// The clustering sidecar's read-time-only class. Offered for rule/report
// authoring only when the sidecar is enabled (GET `clustering_enabled`); an
// already-stored contract_anomaly value still renders because MultiSelect keeps
// values not in its options, so turning the sidecar off never silently drops it.
const CONTRACT_ANOMALY_OPTION = {
	value: "contract_anomaly",
	label: "Contract Anomaly",
};

const EMAIL_DEFAULT: ChannelConfig = { enabled: false, recipients: [] };
const WEBHOOK_DEFAULT: ChannelConfig = { enabled: false, default_url: "" };
const REPORT_DEFAULT: PeriodicReportConfig = {
	enabled: false,
	frequency: "weekly",
	window_days: 7,
	channels: ["email"],
	recipients: [],
	attack_classes: "all",
	min_band: "Moderate",
};

/**
 * Fill in any structural block the editor reads but a stored document might
 * omit (e.g. a hand-crafted/legacy doc missing `periodic_report`), so the page
 * can't crash on render. Stored values always win over the defaults (spread
 * last).
 */
function withDefaults(c: NotificationConfig): NotificationConfig {
	return {
		version: c.version ?? 1,
		channels: {
			...c.channels,
			email: { ...EMAIL_DEFAULT, ...c.channels?.email },
			webhook: { ...WEBHOOK_DEFAULT, ...c.channels?.webhook },
		},
		groups: c.groups ?? {},
		triggers: {
			defaults: c.triggers?.defaults ?? {},
			rules: c.triggers?.rules ?? [],
		},
		periodic_report: { ...REPORT_DEFAULT, ...c.periodic_report },
	};
}

function Toggle({
	checked,
	onChange,
	label,
}: {
	checked: boolean;
	onChange: (v: boolean) => void;
	label: string;
}) {
	return (
		<label className="text-foreground flex cursor-pointer items-center gap-2 text-sm">
			<input
				type="checkbox"
				checked={checked}
				onChange={(e) => onChange(e.target.checked)}
				className="border-border h-4 w-4 rounded-sm border"
			/>
			{label}
		</label>
	);
}

/** Add/remove editor for a list of strings (recipients, group members). */
function StringList({
	values,
	onChange,
	placeholder,
}: {
	values: string[];
	onChange: (next: string[]) => void;
	placeholder: string;
}) {
	const [draft, setDraft] = useState("");
	const add = () => {
		const v = draft.trim();
		if (v && !values.includes(v)) onChange([...values, v]);
		setDraft("");
	};
	return (
		<div className="flex flex-col gap-2">
			<div className="flex flex-wrap items-center gap-1.5">
				{values.length === 0 && (
					<span className="text-muted-foreground text-xs">none</span>
				)}
				{values.map((v) => (
					<Badge key={v} variant="outline" className="gap-1">
						{v}
						<button
							type="button"
							aria-label={`remove ${v}`}
							onClick={() => onChange(values.filter((x) => x !== v))}
						>
							<X className="h-3 w-3" />
						</button>
					</Badge>
				))}
			</div>
			<div className="flex gap-2">
				<Input
					value={draft}
					onChange={(e) => setDraft(e.target.value)}
					placeholder={placeholder}
					onKeyDown={(e) => {
						if (e.key === "Enter") {
							e.preventDefault();
							add();
						}
					}}
					className="h-9 max-w-80"
				/>
				<Button type="button" variant="outline" size="sm" onClick={add}>
					Add
				</Button>
			</div>
		</div>
	);
}

function Section({
	title,
	children,
}: {
	title: string;
	children: React.ReactNode;
}) {
	return (
		<section className="border-border bg-card rounded-lg border-2">
			<header className="border-border border-b px-5 py-3">
				<h2 className="text-foreground text-sm font-semibold">{title}</h2>
			</header>
			<div className="flex flex-col gap-4 px-5 py-4">{children}</div>
		</section>
	);
}

export function NotificationsSettingsPage() {
	const qc = useQueryClient();
	const { data, isPending, isError } = useQuery({
		queryKey: QK,
		queryFn: fetchNotificationConfig,
		staleTime: 30_000,
	});
	const [cfg, setCfg] = useState<NotificationConfig | null>(null);

	// Seed/reset the local editable copy whenever a new config arrives
	const [seededFrom, setSeededFrom] = useState<NotificationConfig | undefined>(
		undefined,
	);
	if (data?.config && data.config !== seededFrom) {
		setSeededFrom(data.config);
		setCfg(withDefaults(structuredClone(data.config)));
	}

	const save = useMutation({
		mutationFn: (c: NotificationConfig) => updateNotificationConfig(c),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: QK });
			toast.success("Notification settings saved");
		},
		onError: (e) => toast.error(e instanceof Error ? e.message : "Save failed"),
	});

	const patch = (fn: (d: NotificationConfig) => void) =>
		setCfg((prev) => {
			if (!prev) return prev;
			const next = structuredClone(prev);
			fn(next);
			return next;
		});

	if (isError)
		return <p className="text-status-offline p-6 text-sm">Failed to load.</p>;
	if (isPending || !cfg)
		return <p className="text-muted-foreground p-6 text-sm">Loading…</p>;

	const channelNames = Object.keys(cfg.channels);
	// contract_anomaly is selectable only when the sidecar is enabled; an already
	// stored value still displays (MultiSelect keeps values outside its options).
	const attackClassOptions = data?.clustering_enabled
		? [...ATTACK_CLASS_OPTIONS, CONTRACT_ANOMALY_OPTION]
		: ATTACK_CLASS_OPTIONS;
	const channelOptions = channelNames.map((c) => ({ value: c, label: c }));
	const warnings = configWarnings(cfg);

	return (
		<div className="flex flex-col gap-4 pb-10">
			<div className="flex items-center justify-between">
				<h1 className="text-foreground text-lg font-semibold">
					Notification Settings
				</h1>
				<Button
					onClick={() => save.mutate(cfg)}
					disabled={save.isPending}
					className="min-w-28"
				>
					{save.isPending ? "Saving…" : "Save"}
				</Button>
			</div>

			{warnings.length > 0 && (
				<div className="border-status-warning/40 bg-status-warning/10 rounded-lg border-2 px-5 py-3">
					<p className="text-status-warning text-xs font-semibold">
						These settings won't deliver as-is
					</p>
					<ul className="text-muted-foreground mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs">
						{warnings.map((w) => (
							<li key={w}>{w}</li>
						))}
					</ul>
				</div>
			)}

			{/* Channels */}
			<Section title="Channels">
				<p className="text-muted-foreground text-xs">
					Turn a channel on here and give it a destination (recipients for
					email, a Default URL for webhook). A channel that is off is skipped
					everywhere: ticking it in a band default or a rule below does nothing
					until it is enabled here.
				</p>
				<div className="flex flex-col gap-2">
					<div className="flex items-center justify-between">
						<Toggle
							checked={!!cfg.channels.email?.enabled}
							onChange={(v) =>
								patch((d) => {
									d.channels.email = { ...d.channels.email, enabled: v };
								})
							}
							label="Email enabled"
						/>
						<span className="text-muted-foreground text-xs">
							SMTP{" "}
							{data?.secrets_status.smtp_configured
								? "configured ✓"
								: "not configured ✗"}
						</span>
					</div>
					<Label className="text-muted-foreground text-xs">
						Default recipients (plain address or `group:&lt;name&gt;`)
					</Label>
					<StringList
						values={cfg.channels.email?.recipients ?? []}
						onChange={(next) =>
							patch((d) => {
								d.channels.email = { ...d.channels.email, recipients: next };
							})
						}
						placeholder="ops@example.com or group:soc-team"
					/>
				</div>

				<div className="border-border flex flex-col gap-2 border-t pt-4">
					<div className="flex items-center justify-between">
						<Toggle
							checked={!!cfg.channels.webhook?.enabled}
							onChange={(v) =>
								patch((d) => {
									d.channels.webhook = { ...d.channels.webhook, enabled: v };
								})
							}
							label="Webhook enabled"
						/>
						<span className="text-muted-foreground text-xs">
							Signing secret{" "}
							{data?.secrets_status.webhook_signing_secret_configured
								? "set ✓"
								: "not set ✗"}
						</span>
					</div>
					<Label className="text-muted-foreground text-xs">
						Default URL (where webhook alerts are POSTed; required unless a rule
						sets its own URL)
					</Label>
					<Input
						value={cfg.channels.webhook?.default_url ?? ""}
						onChange={(e) =>
							patch((d) => {
								d.channels.webhook = {
									...d.channels.webhook,
									enabled: !!d.channels.webhook?.enabled,
									default_url: e.target.value,
								};
							})
						}
						placeholder="https://hooks.example.com/tms"
						className="h-9 max-w-130"
					/>
				</div>
			</Section>

			{/* Groups */}
			<Section title="Groups (recipient aliases)">
				{Object.keys(cfg.groups).length === 0 && (
					<span className="text-muted-foreground text-xs">
						No groups defined.
					</span>
				)}
				{Object.entries(cfg.groups).map(([name, members]) => (
					<div key={name} className="flex flex-col gap-2">
						<div className="flex items-center gap-2">
							<Badge variant="outline">group:{name}</Badge>
							<button
								type="button"
								aria-label={`remove group ${name}`}
								onClick={() =>
									patch((d) => {
										delete d.groups[name];
									})
								}
							>
								<Trash2 className="text-muted-foreground h-4 w-4" />
							</button>
						</div>
						<StringList
							values={members}
							onChange={(next) =>
								patch((d) => {
									d.groups[name] = next;
								})
							}
							placeholder="member@example.com"
						/>
					</div>
				))}
				<AddGroup
					existing={Object.keys(cfg.groups)}
					onAdd={(name) =>
						patch((d) => {
							d.groups[name] = [];
						})
					}
				/>
			</Section>

			{/* Trigger defaults matrix */}
			<Section title="Triggers — defaults (band → channels)">
				<p className="text-muted-foreground text-xs">
					The baseline: which channels fire for each band. Only channels enabled
					under Channels above fire; a column marked (off) is ignored. A matching
					per-class rule below replaces the row for that class.
				</p>
				<div className="overflow-x-auto">
					<table className="text-sm">
						<thead>
							<tr className="text-muted-foreground text-xs">
								<th className="px-3 py-1 text-left">Band</th>
								{channelNames.map((c) => (
									<th key={c} className="px-3 py-1 text-left">
										{c}
										{!cfg.channels[c]?.enabled && (
											<span className="text-status-warning ml-1 font-normal">
												(off)
											</span>
										)}
									</th>
								))}
							</tr>
						</thead>
						<tbody>
							{BANDS.map((band) => {
								const chans = cfg.triggers.defaults[band] ?? [];
								return (
									<tr key={band} className="border-border border-t">
										<td className="text-foreground px-3 py-1.5">{bandLabel(band)}</td>
										{channelNames.map((c) => (
											<td key={c} className="px-3 py-1.5">
												<input
													type="checkbox"
													className="border-border h-4 w-4 rounded-sm border"
													aria-label={`Send ${bandLabel(band)} alerts via ${c}`}
													checked={chans.includes(c)}
													onChange={() =>
														patch((d) => {
															const arr = (d.triggers.defaults[band] ??= []);
															const i = arr.indexOf(c);
															if (i >= 0) arr.splice(i, 1);
															else arr.push(c);
														})
													}
												/>
											</td>
										))}
									</tr>
								);
							})}
						</tbody>
					</table>
				</div>
			</Section>

			{/* Trigger rules */}
			<Section title="Triggers — per-class rules">
				<p className="text-muted-foreground text-xs">
					A rule replaces (not adds to) the band default for its listed attack
					classes; unlisted classes keep the defaults. If several rules match,
					the last wins. Only enabled channels fire, so enable the channel under
					Channels above before relying on a rule.
				</p>
				{cfg.triggers.rules.map((rule, i) => (
					<RuleEditor
						key={i}
						rule={rule}
						channelOptions={channelOptions}
						attackClassOptions={attackClassOptions}
						onChange={(next) =>
							patch((d) => {
								d.triggers.rules[i] = next;
							})
						}
						onRemove={() =>
							patch((d) => {
								d.triggers.rules.splice(i, 1);
							})
						}
					/>
				))}
				<Button
					type="button"
					variant="outline"
					size="sm"
					className="w-fit gap-1"
					onClick={() =>
						patch((d) => {
							d.triggers.rules.push({
								band: "Critical",
								attack_classes: [],
								channels: [],
							});
						})
					}
				>
					<Plus className="h-4 w-4" /> Add rule
				</Button>
			</Section>

			{/* Periodic report */}
			<Section title="Periodic report">
				<Toggle
					checked={cfg.periodic_report.enabled}
					onChange={(v) =>
						patch((d) => {
							d.periodic_report.enabled = v;
						})
					}
					label="Enabled"
				/>
				<div className="flex flex-wrap items-end gap-4">
					<LabeledSelect
						label="Frequency"
						value={cfg.periodic_report.frequency}
						options={FREQUENCIES.map((f) => ({ value: f, label: f }))}
						onChange={(v) =>
							patch((d) => {
								d.periodic_report.frequency = v as never;
							})
						}
					/>
					<div className="flex flex-col gap-1.5">
						<Label className="text-muted-foreground text-xs">
							Window (days)
						</Label>
						<Input
							type="number"
							min={1}
							value={cfg.periodic_report.window_days}
							onChange={(e) =>
								patch((d) => {
									d.periodic_report.window_days = Math.max(
										1,
										Number(e.target.value) || 1,
									);
								})
							}
							className="h-9 w-24"
						/>
					</div>
					<LabeledSelect
						label="Min band"
						value={cfg.periodic_report.min_band}
						options={BANDS.map((b) => ({ value: b, label: bandLabel(b) }))}
						onChange={(v) =>
							patch((d) => {
								d.periodic_report.min_band = v;
							})
						}
					/>
					<div className="flex flex-col gap-1.5">
						<Label className="text-muted-foreground text-xs">Channels</Label>
						<MultiSelect
							options={channelOptions}
							value={cfg.periodic_report.channels}
							onChange={(next) =>
								patch((d) => {
									d.periodic_report.channels = next;
								})
							}
							placeholder="channels"
							label="channel"
						/>
					</div>
				</div>
				<div className="flex flex-col gap-1.5">
					<Label className="text-muted-foreground text-xs">
						Recipients (empty = use the channel's global list)
					</Label>
					<StringList
						values={cfg.periodic_report.recipients}
						onChange={(next) =>
							patch((d) => {
								d.periodic_report.recipients = next;
							})
						}
						placeholder="reports@example.com or group:soc-team"
					/>
				</div>
				<div className="flex flex-col gap-1.5">
					<Toggle
						checked={cfg.periodic_report.attack_classes === "all"}
						onChange={(v) =>
							patch((d) => {
								d.periodic_report.attack_classes = v ? "all" : [];
							})
						}
						label="All attack classes"
					/>
					{cfg.periodic_report.attack_classes !== "all" && (
						<MultiSelect
							options={attackClassOptions}
							value={cfg.periodic_report.attack_classes}
							onChange={(next) =>
								patch((d) => {
									d.periodic_report.attack_classes = next;
								})
							}
							placeholder="attack classes"
							label="class"
							className="w-72"
						/>
					)}
				</div>
			</Section>
		</div>
	);
}

function LabeledSelect({
	label,
	value,
	options,
	onChange,
}: {
	label: string;
	value: string;
	options: { value: string; label: string }[];
	onChange: (v: string) => void;
}) {
	// If the stored value isn't among the options (e.g. a legacy band the UI no
	// longer lists), surface it as a transient option so it renders and
	// round-trips instead of showing blank and being silently overwritten.
	const opts = options.some((o) => o.value === value)
		? options
		: [{ value, label: `${value} (current)` }, ...options];
	return (
		<div className="flex flex-col gap-1.5">
			<Label className="text-muted-foreground text-xs">{label}</Label>
			<Select value={value} onValueChange={onChange}>
				<SelectTrigger className="h-9 w-40">
					<SelectValue />
				</SelectTrigger>
				<SelectContent>
					{opts.map((o) => (
						<SelectItem key={o.value} value={o.value}>
							{o.label}
						</SelectItem>
					))}
				</SelectContent>
			</Select>
		</div>
	);
}

function RuleEditor({
	rule,
	channelOptions,
	attackClassOptions,
	onChange,
	onRemove,
}: {
	rule: TriggerRule;
	channelOptions: { value: string; label: string }[];
	attackClassOptions: { value: string; label: string }[];
	onChange: (next: TriggerRule) => void;
	onRemove: () => void;
}) {
	const emailOverride = rule.recipients?.email ?? [];
	return (
		<div className="border-border flex flex-col gap-3 rounded-md border p-3">
			<div className="flex flex-wrap items-end gap-4">
				<LabeledSelect
					label="Band"
					value={rule.band}
					options={BANDS.map((b) => ({ value: b, label: b }))}
					onChange={(v) => onChange({ ...rule, band: v })}
				/>
				<div className="flex flex-col gap-1.5">
					<Label className="text-muted-foreground text-xs">
						Attack classes
					</Label>
					<MultiSelect
						options={attackClassOptions}
						value={rule.attack_classes}
						onChange={(next) => onChange({ ...rule, attack_classes: next })}
						placeholder="classes"
						label="class"
						className="w-64"
					/>
				</div>
				<div className="flex flex-col gap-1.5">
					<Label className="text-muted-foreground text-xs">Channels</Label>
					<MultiSelect
						options={channelOptions}
						value={rule.channels}
						onChange={(next) => onChange({ ...rule, channels: next })}
						placeholder="channels"
						label="channel"
					/>
				</div>
				<button type="button" aria-label="remove rule" onClick={onRemove}>
					<Trash2 className="text-muted-foreground h-4 w-4" />
				</button>
			</div>
			<div className="flex flex-col gap-1.5">
				<Label className="text-muted-foreground text-xs">
					Webhook URL override (optional)
				</Label>
				<Input
					value={rule.webhook_url ?? ""}
					onChange={(e) =>
						onChange({ ...rule, webhook_url: e.target.value || undefined })
					}
					placeholder="https://hooks.example.com/tms-critical"
					className="h-9 max-w-130"
				/>
			</div>
			<div className="flex flex-col gap-1.5">
				<Label className="text-muted-foreground text-xs">
					Email recipients override (optional)
				</Label>
				<StringList
					values={emailOverride}
					onChange={(next) => {
						const recipients = { ...(rule.recipients ?? {}) };
						if (next.length) recipients.email = next;
						else delete recipients.email;
						onChange({
							...rule,
							recipients: Object.keys(recipients).length
								? recipients
								: undefined,
						});
					}}
					placeholder="ciso@example.com or group:soc-team"
				/>
			</div>
		</div>
	);
}

function AddGroup({
	existing,
	onAdd,
}: {
	existing: string[];
	onAdd: (name: string) => void;
}) {
	const [name, setName] = useState("");
	return (
		<div className="border-border flex items-end gap-2 border-t pt-4">
			<div className="flex flex-col gap-1.5">
				<Label className="text-muted-foreground text-xs">New group name</Label>
				<Input
					value={name}
					onChange={(e) => setName(e.target.value)}
					placeholder="soc-team"
					className="h-9 w-60"
				/>
			</div>
			<Button
				type="button"
				variant="outline"
				size="sm"
				disabled={!name.trim() || existing.includes(name.trim())}
				onClick={() => {
					onAdd(name.trim());
					setName("");
				}}
			>
				Add group
			</Button>
		</div>
	);
}
