import { DonutCard } from "@/components/attack-detail/donut";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRiskAlert } from "@/lib/api/analysis";
import { getNetwork } from "@/lib/api/fetch";
import {
	useArchiveMeta,
	useArchiveMutation,
	useIsArchived,
	useRestoreMutation,
	type ArchiveMeta,
} from "@/lib/archive-store";
import { useAuth } from "@/lib/auth";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import type { RiskAlert } from "@/mocks/attacks";
import {
	ARCHIVE_REASONS,
	ATTACK_META,
	SUB_SCORE_LABELS,
} from "@/mocks/attacks";
import {
	AlertTriangle,
	ArrowDown,
	ArrowUp,
	Copy,
	ExternalLink,
	Info,
	RotateCcw,
	Trash2,
	X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";

export function AttackDetailPage({ archived = false }: { archived?: boolean }) {
	const navigate = useNavigate();
	const { id } = useParams<{ id: string }>();
	const { data: alert, isPending, isError } = useRiskAlert(id);
	const archivedHere = useIsArchived(id);

	// `archivedHere === undefined` means the archive lookup is still in flight.
	// We can't decide the redirect until we know, otherwise a deep link to
	// `/archive/:id` would briefly bounce to `/attacks/:id` and back.
	if (isPending || archivedHere === undefined) {
		return (
			<div className="border-border bg-card text-muted-foreground rounded-lg border-2 p-8 text-center text-sm">
				Loading attack…
			</div>
		);
	}

	if (isError || !alert) {
		return (
			<div className="border-border bg-card rounded-lg border-2 p-8 text-center">
				<h2 className="text-foreground text-lg font-semibold">
					Attack not found
				</h2>
				<p className="text-muted-foreground mt-2 text-sm">
					The alert <code className="font-mono">{id}</code> does not exist.
				</p>
				<Link
					to={archived ? "/archive" : "/dashboard"}
					className="border-border text-foreground hover:bg-accent mt-4 inline-flex h-10 items-center justify-center rounded-md border px-4 text-sm font-medium"
				>
					Back
				</Link>
			</div>
		);
	}

	// If user navigates to /attacks/:id of an archived alert, redirect to /archive/:id
	if (!archived && archivedHere) {
		return <Navigate to={`/archive/${alert.slug}`} replace />;
	}
	// If user navigates to /archive/:id of an active alert, redirect to /attacks/:id
	if (archived && !archivedHere) {
		return <Navigate to={`/attacks/${alert.slug}`} replace />;
	}

	console.log({ alert, archived, archivedHere });
	return (
		<DetailCard
			alert={alert}
			archived={archived}
			onClose={() => navigate(-1)}
			onArchived={() => navigate("/archive", { replace: true })}
			onRestored={() => navigate("/dashboard", { replace: true })}
		/>
	);
}

function DetailCard({
	alert,
	archived,
	onClose,
	onArchived,
	onRestored,
}: {
	alert: RiskAlert;
	archived: boolean;
	onClose: () => void;
	onArchived: () => void;
	onRestored: () => void;
}) {
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [restoreOpen, setRestoreOpen] = useState(false);
	const meta = ATTACK_META[alert.attackType];
	const Icon = ATTACK_ICON[alert.attackType];
	const archiveMeta = useArchiveMeta(archived ? alert.slug : undefined);
	const { mutate: archiveAlert, isPending: archivePending } =
		useArchiveMutation();
	const { mutate: restoreAlert, isPending: restorePending } =
		useRestoreMutation();
	const { user } = useAuth();

	const analyzed = alert.date;

	return (
		<section className="border-border bg-card rounded-lg border-2">
			{/* Header */}
			<header className="flex items-center justify-between gap-3 px-5 py-3">
				<h1 className="text-foreground text-base font-semibold">
					{archived ? "Archived Attack Detail" : "Attack Detail"}
				</h1>
				<div className="text-muted-foreground flex items-center gap-1">
					{archived ? (
						<button
							type="button"
							onClick={() => setRestoreOpen(true)}
							className="border-border text-foreground hover:bg-accent focus-visible:ring-ring inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none"
						>
							<RotateCcw className="h-3.5 w-3.5" />
							Restore
						</button>
					) : (
						<IconButton title="Delete" onClick={() => setDeleteOpen(true)}>
							<Trash2 className="h-4 w-4" />
						</IconButton>
					)}
					<IconButton title="Open externally">
						<ExternalLink className="h-4 w-4" />
					</IconButton>
					<IconButton title="Close" onClick={onClose}>
						<X className="h-4 w-4" />
					</IconButton>
				</div>
			</header>

			<Divider />

			{/* Identity */}
			<div className="flex flex-wrap items-center gap-4 px-5 py-4">
				<div className="text-brand flex items-center gap-2">
					<Icon className="h-5 w-5" />
					<span className="text-base font-semibold">{alert.attackType}:</span>
					<button
						type="button"
						title={meta.description}
						className="text-muted-foreground/80 hover:text-foreground"
					>
						<Info className="h-4 w-4" />
					</button>
				</div>
				<div className="text-foreground flex items-center gap-2 font-mono text-sm">
					<span>{alert.fullHash}</span>
					<button
						type="button"
						className="text-muted-foreground hover:text-foreground"
						title="Copy"
					>
						<Copy className="h-3.5 w-3.5" />
					</button>
				</div>
				<Badge
					variant={SEVERITY_VARIANT[alert.severity]}
					className="px-3 text-sm"
				>
					{alert.severity}
				</Badge>
			</div>

			<Divider />

			{/* Archive reason — only shown when archived */}
			{archived && archiveMeta && (
				<>
					<ArchiveReasonRow meta={archiveMeta} />
					<Divider />
				</>
			)}

			{/* Transactions metrics */}
			<Section title="Transactions">
				<MetricsTwoCol
					left={[
						{ label: "RISK SCORE", value: `${alert.riskScore}/100` },
						{ label: "FEE", value: `${alert.feeAda.toFixed(2)} ADA` },
					]}
					right={[
						{ label: "ANALYZED", value: analyzed },
						{ label: "OUTPUTS", value: String(alert.outputs) },
					]}
				/>
			</Section>

			<Divider />

			{/* Type-specific section */}
			<AttackTypeSection alert={alert} />

			<Divider />

			{/* Sub-scores */}
			<Section title="Sub-scores">
				<SubScores alert={alert} />
			</Section>

			{!archived && (
				<DeleteDialog
					open={deleteOpen}
					onOpenChange={setDeleteOpen}
					confirmDisabled={archivePending}
					onConfirm={(reason, notes) => {
						// Backend has a single free-text `note` field. The UI keeps the
						// "Reason" dropdown + "Notes" textarea separate (per Figma) and
						// we collapse them on submit. Empty notes degrade to just the
						// reason; empty reason shouldn't happen (dropdown is required).
						const note = notes.trim() ? `${reason}: ${notes.trim()}` : reason;
						archiveAlert(
							{
								network: getNetwork(),
								tx_hash: alert.slug,
								note,
								archived_by: user?.email ?? "Unknown",
							},
							{
								onSuccess: () => {
									setDeleteOpen(false);
									onArchived();
								},
							},
						);
					}}
				/>
			)}
			{archived && (
				<RestoreDialog
					open={restoreOpen}
					onOpenChange={setRestoreOpen}
					confirmDisabled={restorePending}
					onConfirm={() => {
						restoreAlert(
							{ txHash: alert.slug },
							{
								onSuccess: () => {
									setRestoreOpen(false);
									onRestored();
								},
							},
						);
					}}
				/>
			)}
		</section>
	);
}

function ArchiveReasonRow({ meta }: { meta: ArchiveMeta }) {
	// Backend stores a single `note` field. The UI composes it as
	// "{Reason}: {free-text notes}" on archive — split here so the tooltip
	// renders the same reason / notes structure that was entered.
	const splitIdx = meta.note.indexOf(": ");
	const reason = splitIdx > 0 ? meta.note.slice(0, splitIdx) : meta.note;
	const notes = splitIdx > 0 ? meta.note.slice(splitIdx + 2) : "";

	return (
		<div className="flex items-baseline gap-6 px-5 py-3">
			<span className="text-brand text-sm font-semibold">
				Archive Reason & Notes:
			</span>
			<Tooltip>
				<TooltipTrigger asChild>
					<span className="text-brand min-w-0 flex-1 cursor-help truncate text-right text-sm">
						{meta.note}
					</span>
				</TooltipTrigger>
				<TooltipContent side="bottom" align="end" className="max-w-md">
					<div className="space-y-1">
						<div className="text-foreground font-semibold">{reason}</div>
						{notes && (
							<div className="text-muted-foreground whitespace-pre-line">
								{notes}
							</div>
						)}
						<div className="text-muted-foreground pt-1 text-[11px]">
							by {meta.archived_by}
							{meta.source && meta.source !== "local"
								? ` · ${meta.source}`
								: ""}
						</div>
					</div>
				</TooltipContent>
			</Tooltip>
		</div>
	);
}

function RestoreDialog({
	open,
	onOpenChange,
	onConfirm,
	confirmDisabled,
}: {
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onConfirm: () => void;
	confirmDisabled?: boolean;
}) {
	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent showClose={false} className="max-w-sm">
				<DialogHeader>
					<DialogTitle>
						Are you sure you want to restore this attack?
					</DialogTitle>
				</DialogHeader>
				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						Cancel
					</Button>
					<Button
						onClick={onConfirm}
						disabled={confirmDisabled}
						className="border-border text-brand hover:bg-accent hover:text-brand border bg-transparent"
					>
						{confirmDisabled ? "Restoring…" : "Confirm"}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

/**
 * Renders the 4–5 sub-score donuts for an alert. Pulls live values from the
 * backend (`alert.subScores`, 0..1 normalized) and falls back to the hardcoded
 * `ATTACK_META.subScores` percentages when a dimension is missing.
 */
function SubScores({ alert }: { alert: RiskAlert }) {
	const labels = SUB_SCORE_LABELS[alert.attackType];
	const fallback = ATTACK_META[alert.attackType].subScores;
	const cards = labels.map((entry, i) => {
		const raw = alert.subScores?.[entry.key];
		const percent =
			typeof raw === "number"
				? Math.round(Math.max(0, Math.min(1, raw)) * 100)
				: (fallback[i]?.percent ?? 0);
		return {
			label: entry.label,
			percent,
			description: entry.description,
		};
	});
	return (
		<div
			className={cn(
				"grid gap-3",
				cards.length === 5 ? "md:grid-cols-5" : "md:grid-cols-4",
			)}
		>
			{cards.map((c) => (
				<DonutCard
					key={c.label}
					label={c.label}
					percent={c.percent}
					description={c.description}
				/>
			))}
		</div>
	);
}

const EVIDENCE_PLACEHOLDER = "—";

function ev<T = unknown>(alert: RiskAlert, key: string): T | undefined {
	const v = alert.evidence?.[key];
	return v === undefined || v === null ? undefined : (v as T);
}

function fmtNumber(n: number | undefined): string {
	if (n === undefined || !Number.isFinite(n)) return EVIDENCE_PLACEHOLDER;
	return new Intl.NumberFormat("en-US").format(n);
}

function fmtBytes(n: number | undefined): string {
	if (n === undefined || !Number.isFinite(n)) return EVIDENCE_PLACEHOLDER;
	return `${fmtNumber(n)} bytes`;
}

function fmtLovelaceAsAda(lov: number | undefined, digits = 2): string {
	if (lov === undefined || !Number.isFinite(lov)) return EVIDENCE_PLACEHOLDER;
	return `${(lov / 1_000_000).toFixed(digits)} ADA`;
}

function fmtAddress(addr: string | undefined, head = 12, tail = 8): string {
	if (!addr) return EVIDENCE_PLACEHOLDER;
	if (addr.length <= head + tail + 3) return addr;
	return `${addr.slice(0, head)}…${addr.slice(-tail)}`;
}

function fmtTxHash(hash: string | undefined): string {
	return fmtAddress(hash, 8, 8);
}

function fmtPct(ratio: number | undefined, digits = 1): string {
	if (ratio === undefined || !Number.isFinite(ratio)) return EVIDENCE_PLACEHOLDER;
	return `${(ratio * 100).toFixed(digits)}%`;
}

function fmtBool(b: boolean | undefined): string {
	if (b === undefined) return EVIDENCE_PLACEHOLDER;
	return b ? "Yes" : "No";
}

function fmtAssetName(hex: string | undefined, ascii: string | undefined): string {
	if (ascii) return ascii;
	if (hex) return hex.length > 32 ? `${hex.slice(0, 32)}…` : hex;
	return EVIDENCE_PLACEHOLDER;
}

function AttackTypeSection({ alert }: { alert: RiskAlert }) {
	switch (alert.attackType) {
		case "Phishing": {
			const urls = (ev<unknown[]>(alert, "urls") ?? []) as Array<{
				url: string;
				severity?: string;
				phishing_tld?: boolean;
			}>;
			const severity = ev<string>(alert, "severity") ?? EVIDENCE_PLACEHOLDER;
			const recipientCount = ev<number>(alert, "recipient_count");
			const labels = ev<string[]>(alert, "metadata_labels") ?? [];
			const metadataLabel = labels.length
				? labels.map((l) => `label ${l}`).join(", ")
				: EVIDENCE_PLACEHOLDER;
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Extracted URLs">
								{urls.length === 0 ? (
									<div className="text-muted-foreground text-sm">
										No URLs extracted.
									</div>
								) : (
									urls.map((u, i) => (
										<div key={`${u.url}-${i}`}>
											<UrlRow
												variant={
													u.severity === "BLACKLISTED"
														? "danger"
														: u.severity === "SUSPICIOUS"
															? "warn"
															: undefined
												}
												label={u.severity}
												url={u.url}
												meta={u.phishing_tld ? "Phishing-prone TLD" : undefined}
											/>
											{i < urls.length - 1 && <Divider />}
										</div>
									))
								)}
							</Stack>
						}
						right={
							<Stack title="Delivery Analysis">
								<KeyVal label="SEVERITY" value={severity} />
								<KeyVal label="SE TIER" value={EVIDENCE_PLACEHOLDER} />
								<KeyVal label="RECIPIENTS" value={fmtNumber(recipientCount)} />
								<KeyVal label="METADATA LABEL" value={metadataLabel} />
							</Stack>
						}
					/>
				</Section>
			);
		}

		case "Fake Token": {
			const matchedToken = ev<string>(alert, "matched_token") ?? "";
			const fakePolicyId = ev<string>(alert, "fake_policy_id");
			const legitPolicies = ev<string[]>(alert, "legit_policy_ids") ?? [];
			const cip25Sim = ev<number>(alert, "cip25_similarity_raw");
			const recipientCount = ev<number>(alert, "recipient_count");
			const confusables =
				(ev<unknown[]>(alert, "unicode_confusables") ?? []) as Array<{
					kind?: "homoglyph" | "zero_width" | "mixed_script";
					from_char: string;
					to_char: string;
				}>;
			const describeConfusable = (c: {
				kind?: string;
				from_char: string;
				to_char: string;
			}): string => {
				if (c.kind === "zero_width") {
					return `Zero-width character: ${c.from_char}`;
				}
				if (c.kind === "mixed_script") {
					return `Mixed scripts: ${c.from_char}`;
				}
				// Default and ``homoglyph`` kind both read as visual substitution.
				return `'${c.from_char}' replacing '${c.to_char}'`;
			};
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Token Comparison">
								<KeyVal
									label="FAKE TOKEN"
									value={
										<span className="flex flex-wrap items-center justify-end gap-3">
											<span className="text-muted-foreground">
												Age: {EVIDENCE_PLACEHOLDER}
											</span>
											<span className="font-mono text-sm">
												{fmtAddress(fakePolicyId)}
											</span>
										</span>
									}
								/>
								<KeyVal
									label={`REAL TOKEN${matchedToken ? ` (${matchedToken})` : ""}`}
									value={
										<span className="font-mono text-sm">
											{fmtAddress(legitPolicies[0])}
										</span>
									}
								/>
							</Stack>
						}
						right={
							<Stack title="Distribution">
								<KeyVal label="CIP-25 METADATA MATCH" value={fmtPct(cip25Sim)} />
								<KeyVal label="RECIPIENTS" value={fmtNumber(recipientCount)} />
							</Stack>
						}
					/>
					<div className="mt-6">
						<Stack title="Unicode Analysis">
							{confusables.length === 0 ? (
								<div className="text-muted-foreground text-sm">
									No confusable characters detected.
								</div>
							) : (
								<div className="grid gap-3 md:grid-cols-2">
									{confusables.map((c, i) => (
										<UnicodeWarning key={i} text={describeConfusable(c)} />
									))}
								</div>
							)}
						</Stack>
					</div>
				</Section>
			);
		}

		case "Circular": {
			const hops =
				(ev<unknown[]>(alert, "hops") ?? []) as Array<{
					address: string;
					amount_lovelace: number;
					slot: number;
				}>;
			const amountSim = ev<number>(alert, "amount_similarity_raw");
			const netLoss = ev<number>(alert, "net_loss_ratio");
			const firstSlot = ev<number>(alert, "first_slot");
			const cycleLen = ev<number>(alert, "cycle_length");
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Circular Transfer">
								{hops.length === 0 ? (
									<div className="text-muted-foreground text-sm">
										Hop chain not available.
									</div>
								) : (
									<FlowChain
										direction="down"
										rows={hops.map((h, i) => ({
											label: `HOP ${i + 1}`,
											amount: fmtLovelaceAsAda(h.amount_lovelace),
											address: fmtAddress(h.address),
										}))}
									/>
								)}
							</Stack>
						}
						right={
							<Stack title="Cycle Metrics">
								<KeyVal label="AMOUNT SIMILARITY" value={fmtPct(amountSim)} />
								<KeyVal label="NET LOSS" value={fmtPct(netLoss)} />
								<KeyVal
									label="FIRST SLOT"
									value={firstSlot ? fmtNumber(firstSlot) : EVIDENCE_PLACEHOLDER}
								/>
								<KeyVal
									label="CYCLE LENGTH"
									value={
										cycleLen !== undefined
											? `${cycleLen} HOPS`
											: EVIDENCE_PLACEHOLDER
									}
								/>
							</Stack>
						}
					/>
				</Section>
			);
		}

		case "Sandwich": {
			const poolId = ev<string>(alert, "pool_id");
			const assetPair = ev<string>(alert, "asset_pair");
			const rateDeltaPct = ev<number>(alert, "rate_delta_pct");
			const profit = ev<number>(alert, "attacker_profit_lovelace");
			const slotSpan = ev<number>(alert, "slot_span");
			const txA = ev<string>(alert, "tx_a_hash");
			const txB = ev<string>(alert, "tx_b_hash");
			const swapRateVictim = ev<number>(alert, "swap_rate_victim");
			const swapRateBaseline = ev<number>(alert, "swap_rate_baseline");
			const fmtRate = (r: number | undefined) =>
				r !== undefined && Number.isFinite(r) && r > 0
					? `rate ${r.toFixed(4)}`
					: EVIDENCE_PLACEHOLDER;
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Sandwich Attack Flow">
								<div className="space-y-1">
									{/* Flow rows render a single signal in the amount
									    column for consistency: the swap rate at each leg.
									    Profit (lovelace) sits in "Attack Details" on the
									    right so we don't mix rates and ADA in one column. */}
									<Row
										color="online"
										label="FRONT RUN (tx_A)"
										amount={fmtRate(swapRateBaseline)}
										address={fmtTxHash(txA)}
									/>
									<ArrowsRow direction="down" />
									<Row
										color="offline"
										label="VICTIM"
										amount={fmtRate(swapRateVictim)}
										address={fmtTxHash(alert.fullHash)}
									/>
									<ArrowsRow direction="up" />
									<Row
										color="online"
										label="BACK RUN (tx_B)"
										amount={EVIDENCE_PLACEHOLDER}
										address={fmtTxHash(txB)}
									/>
								</div>
							</Stack>
						}
						right={
							<Stack title="Attack Details">
								<KeyVal label="DEX POOL" value={fmtAddress(poolId)} />
								<KeyVal label="ASSET PAIR" value={assetPair || EVIDENCE_PLACEHOLDER} />
								<KeyVal
									label="RATE IMPACT"
									value={
										rateDeltaPct !== undefined
											? `${rateDeltaPct.toFixed(2)}%`
											: EVIDENCE_PLACEHOLDER
									}
								/>
								<KeyVal label="ATTACKER PROFIT" value={fmtLovelaceAsAda(profit)} />
								<KeyVal
									label="SLOT SPAN"
									value={
										slotSpan !== undefined
											? `${slotSpan} SLOTS`
											: EVIDENCE_PLACEHOLDER
									}
								/>
							</Stack>
						}
					/>
				</Section>
			);
		}

		case "Front Running": {
			const counterpart = ev<string>(alert, "counterpart_tx_hash");
			const outcome = ev<string>(alert, "outcome");
			const deltaMs = ev<number>(alert, "delta_ms");
			const sharedInputs = ev<number>(alert, "shared_input_count");
			const txFee = ev<number>(alert, "tx_fee");
			const counterpartFee = ev<number>(alert, "counterpart_fee");
			const txRole = ev<string>(alert, "tx_role"); // "TX_A" | "TX_B" | ""
			// `outcome` tells us which side of the collision won; `tx_role`
			// tells us which side the current alert tx is. Need both to label
			// winner / loser without flipping the badges 50% of the time.
			const meWon =
				(outcome === "TX_A_CONFIRMED" && txRole === "TX_A") ||
				(outcome === "TX_B_CONFIRMED" && txRole === "TX_B");
			const decided =
				(outcome === "TX_A_CONFIRMED" || outcome === "TX_B_CONFIRMED") &&
				(txRole === "TX_A" || txRole === "TX_B");
			const winnerHash = decided
				? meWon
					? alert.fullHash
					: counterpart
				: undefined;
			const loserHash = decided
				? meWon
					? counterpart
					: alert.fullHash
				: undefined;
			const winnerFee = decided ? (meWon ? txFee : counterpartFee) : undefined;
			const loserFee = decided ? (meWon ? counterpartFee : txFee) : undefined;
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Collision Details">
								{decided ? (
									<>
										<KeyVal
											label={<span className="text-status-online">WINNER</span>}
											value={
												<span className="text-status-online font-mono text-sm">
													{fmtTxHash(winnerHash)}
												</span>
											}
										/>
										<KeyVal
											label={<span className="text-status-online">Winner Fee</span>}
											value={
												<span className="text-status-online">
													{fmtLovelaceAsAda(winnerFee)}
												</span>
											}
										/>
										<div className="flex justify-start py-1 pl-1">
											<ArrowDown className="text-brand h-4 w-4" />
										</div>
										<KeyVal
											label={<span className="text-status-offline">LOSER</span>}
											value={
												<span className="text-status-offline font-mono text-sm">
													{fmtTxHash(loserHash)}
												</span>
											}
										/>
										<KeyVal
											label={<span className="text-status-offline">Loser Fee</span>}
											value={
												<span className="text-status-offline">
													{fmtLovelaceAsAda(loserFee)}
												</span>
											}
										/>
									</>
								) : (
									<div className="text-muted-foreground text-sm">
										Outcome undetermined: tx role not recorded yet.
									</div>
								)}
							</Stack>
						}
						right={
							<Stack title="Race Metrics">
								<KeyVal label="SHARED INPUTS" value={fmtNumber(sharedInputs)} />
								<KeyVal
									label="MEMPOOL DELTA"
									value={
										deltaMs !== undefined
											? `${fmtNumber(Math.round(deltaMs))} ms`
											: EVIDENCE_PLACEHOLDER
									}
								/>
								<KeyVal label="OUTCOME" value={outcome ?? EVIDENCE_PLACEHOLDER} />
								<KeyVal label="ATTACKER WINS (24h)" value={EVIDENCE_PLACEHOLDER} />
							</Stack>
						}
					/>
				</Section>
			);
		}

		case "Multiple Sat": {
			const nInputs = ev<number>(alert, "n_inputs_same_script");
			const lovelaceFullDrain = ev<boolean>(alert, "lovelace_full_drain");
			const assetsExtracted = ev<number>(alert, "n_assets_extracted");
			const redeemerCount = ev<number>(alert, "redeemer_count");
			const redeemerRatio = ev<number>(alert, "redeemer_input_ratio");
			const valueExtracted = ev<number>(alert, "value_extracted_lovelace");
			const valueReturned = ev<number>(alert, "value_returned_lovelace");
			const cpuTotal = ev<number>(alert, "cpu_units_total");
			const targetScript = ev<string>(alert, "target_script_address");
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Exploit Pattern">
								<KeyVal label="SCRIPT INPUTS" value={fmtNumber(nInputs)} />
								<KeyVal
									label="LOVELACE FULL DRAIN"
									value={fmtBool(lovelaceFullDrain)}
								/>
								<KeyVal
									label="ASSETS EXTRACTED"
									value={fmtNumber(assetsExtracted)}
								/>
								<KeyVal label="REDEEMERS USED" value={fmtNumber(redeemerCount)} />
								<KeyVal label="REDEEMER RATIO" value={fmtPct(redeemerRatio)} />
							</Stack>
						}
						right={
							<Stack title="Value Flow">
								<KeyVal
									label="VALUE EXTRACTED"
									value={fmtLovelaceAsAda(valueExtracted)}
								/>
								<KeyVal
									label="VALUE RETURNED"
									value={fmtLovelaceAsAda(valueReturned)}
								/>
								<KeyVal label="CPU UNITS" value={fmtNumber(cpuTotal)} />
								<KeyVal
									label="TARGET SCRIPT"
									value={
										<span className="font-mono text-sm">
											{fmtAddress(targetScript)}
										</span>
									}
								/>
							</Stack>
						}
					/>
				</Section>
			);
		}

		case "Large Datum": {
			const datumBytes = ev<number>(alert, "datum_bytes_raw");
			const utxoBytes = ev<number>(alert, "utxo_total_bytes");
			const datumType = ev<string>(alert, "datum_type");
			const targetScript = ev<string>(alert, "target_script_address");
			const ratio = ev<number>(alert, "datum_utxo_ratio");
			const datumPct =
				ratio !== undefined ? Math.round(ratio * 100) : undefined;
			return (
				<Section>
					<Stack title="Large Datum Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="DATUM SIZE" value={fmtBytes(datumBytes)} />
									<Divider />
									<KeyVal label="UTXO SIZE" value={fmtBytes(utxoBytes)} />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal
										label="DATUM TYPE"
										value={
											datumType
												? datumType[0].toUpperCase() + datumType.slice(1)
												: EVIDENCE_PLACEHOLDER
										}
									/>
									<Divider />
									<KeyVal
										label="TARGET SCRIPT"
										value={
											<span className="font-mono text-sm">
												{fmtAddress(targetScript)}
											</span>
										}
									/>
								</div>
							}
						/>
					</Stack>
					{datumPct !== undefined && (
						<div className="mt-6">
							<Stack title="DATUM/UTXO Ratio">
								<RatioBar
									parts={[
										{
											label: `${datumPct}% DATUM`,
											percent: datumPct,
											tone: "brand",
										},
										{
											label: `${100 - datumPct}% UTXO`,
											percent: 100 - datumPct,
											tone: "muted",
										},
									]}
								/>
							</Stack>
						</div>
					)}
				</Section>
			);
		}

		case "Token Dust": {
			const uniqueAssets = ev<number>(alert, "unique_asset_count");
			const cborBytes = ev<number>(alert, "value_cbor_bytes_raw");
			const policyCount = ev<number>(alert, "policy_count");
			const maxPerPolicy = ev<number>(alert, "max_assets_per_policy");
			const targetScript = ev<string>(alert, "target_script_address");
			const lovelace = ev<number>(alert, "lovelace_amount");
			return (
				<Section>
					<Stack title="Dust Attack Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="UNIQUE ASSETS" value={fmtNumber(uniqueAssets)} />
									<Divider />
									<KeyVal label="CBOR SIZE" value={fmtBytes(cborBytes)} />
									<Divider />
									<KeyVal label="POLICY IDS" value={fmtNumber(policyCount)} />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal
										label="MAX ASSETS / POLICY"
										value={fmtNumber(maxPerPolicy)}
									/>
									<Divider />
									<KeyVal
										label="TARGET SCRIPT"
										value={
											<span className="font-mono text-sm">
												{fmtAddress(targetScript)}
											</span>
										}
									/>
									<Divider />
									<KeyVal label="ADA AMOUNT" value={fmtLovelaceAsAda(lovelace)} />
								</div>
							}
						/>
					</Stack>
				</Section>
			);
		}

		case "Large Value": {
			const qtyDigits = ev<number>(alert, "quantity_digits_raw");
			const cborBytes = ev<number>(alert, "value_cbor_bytes_raw");
			const hex = ev<string>(alert, "asset_name_hex");
			const ascii = ev<string>(alert, "asset_name_ascii");
			const maxQty = ev<number>(alert, "max_quantity_raw");
			const policyId = ev<string>(alert, "policy_id");
			const lovelace = ev<number>(alert, "lovelace_amount");
			return (
				<Section>
					<Stack title="Large Value Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="QUANTITY DIGITS" value={fmtNumber(qtyDigits)} />
									<Divider />
									<KeyVal label="CBOR SIZE" value={fmtBytes(cborBytes)} />
									<Divider />
									<KeyVal label="ASSET NAME" value={fmtAssetName(hex, ascii)} />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal label="MAX QUANTITY" value={fmtNumber(maxQty)} />
									<Divider />
									<KeyVal
										label="POLICY ID"
										value={
											<span className="font-mono text-sm">
												{fmtAddress(policyId)}
											</span>
										}
									/>
									<Divider />
									<KeyVal label="ADA AMOUNT" value={fmtLovelaceAsAda(lovelace)} />
								</div>
							}
						/>
					</Stack>
				</Section>
			);
		}
	}
}

/* ---------- Layout primitives ---------- */

function Section({
	title,
	children,
}: {
	title?: string;
	children: React.ReactNode;
}) {
	return (
		<div className="px-5 py-5">
			{title && (
				<h2 className="text-foreground mb-4 text-base font-semibold">
					{title}
				</h2>
			)}
			{children}
		</div>
	);
}

function Stack({
	title,
	children,
}: {
	title: string;
	children: React.ReactNode;
}) {
	return (
		<div>
			<h3 className="text-foreground mb-3 text-sm font-semibold">{title}</h3>
			<div className="space-y-3">{children}</div>
		</div>
	);
}

function Divider() {
	return <div className="bg-border h-px" />;
}

function TwoCol({
	left,
	right,
}: {
	left: React.ReactNode;
	right: React.ReactNode;
}) {
	return (
		<div className="grid gap-x-10 gap-y-6 md:grid-cols-2">
			<div>{left}</div>
			<div>{right}</div>
		</div>
	);
}

function MetricsTwoCol({
	left,
	right,
}: {
	left: { label: string; value: string }[];
	right: { label: string; value: string }[];
}) {
	return (
		<div className="grid gap-x-10 gap-y-3 md:grid-cols-2">
			<div className="space-y-3">
				{left.map((m, i) => (
					<div key={i}>
						<KeyVal label={m.label} value={m.value} />
						{i < left.length - 1 && (
							<div className="mt-3">
								<Divider />
							</div>
						)}
					</div>
				))}
			</div>
			<div className="space-y-3">
				{right.map((m, i) => (
					<div key={i}>
						<KeyVal label={m.label} value={m.value} />
						{i < right.length - 1 && (
							<div className="mt-3">
								<Divider />
							</div>
						)}
					</div>
				))}
			</div>
		</div>
	);
}

function KeyVal({
	label,
	value,
}: {
	label: React.ReactNode;
	value: React.ReactNode;
}) {
	return (
		<div className="flex items-start justify-between gap-4">
			<div className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
				{label}
			</div>
			<div className="text-foreground text-right text-sm">{value}</div>
		</div>
	);
}

function IconButton({
	children,
	title,
	onClick,
}: {
	children: React.ReactNode;
	title?: string;
	onClick?: () => void;
}) {
	return (
		<button
			type="button"
			title={title}
			onClick={onClick}
			className="text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-ring rounded-md p-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
		>
			{children}
		</button>
	);
}

function UrlRow({
	variant,
	label,
	url,
	meta,
}: {
	variant?: "warn" | "danger";
	label?: string;
	url: string;
	meta?: string;
}) {
	return (
		<div>
			{label && (
				<div
					className={cn(
						"mb-1 flex items-center gap-2 text-xs font-medium tracking-wide uppercase",
						variant === "danger"
							? "text-status-offline"
							: variant === "warn"
								? "text-status-warning"
								: "text-muted-foreground",
					)}
				>
					{variant && <AlertTriangle className="h-3.5 w-3.5" />}
					{label}
				</div>
			)}
			<div className="flex items-baseline justify-between gap-4">
				<span className="text-foreground truncate font-mono text-sm">
					{url}
				</span>
				{meta && (
					<span className="text-muted-foreground shrink-0 text-xs">{meta}</span>
				)}
			</div>
		</div>
	);
}

function UnicodeWarning({ text }: { text: string }) {
	return (
		<div className="text-foreground flex items-center gap-2 text-sm">
			<AlertTriangle className="text-status-offline h-4 w-4" />
			{text}
		</div>
	);
}

function RatioBar({
	parts,
}: {
	parts: { label: string; percent: number; tone: "brand" | "muted" }[];
}) {
	return (
		<div className="border-border flex h-9 w-full overflow-hidden rounded-md border">
			{parts.map((p, i) => (
				<div
					key={i}
					style={{ width: `${p.percent}%` }}
					className={cn(
						"flex items-center justify-center text-xs font-semibold",
						p.tone === "brand"
							? "bg-brand text-primary-foreground"
							: "bg-muted text-foreground",
					)}
				>
					{p.label}
				</div>
			))}
		</div>
	);
}

const FLOW_GRID = "grid grid-cols-3 items-center gap-3";

function ArrowsRow({ direction }: { direction: "down" | "up" }) {
	const Arrow = direction === "down" ? ArrowDown : ArrowUp;
	return (
		<div className={cn(FLOW_GRID, "text-brand py-2")}>
			<Arrow className="h-4 w-4 justify-self-center" />
			<Arrow className="h-4 w-4 justify-self-center" />
			<Arrow className="h-4 w-4 justify-self-center" />
		</div>
	);
}

function FlowChain({
	rows,
	direction = "down",
}: {
	rows: { label: string; amount: string; address: string }[];
	direction?: "down" | "up";
}) {
	return (
		<div className="space-y-1">
			{rows.map((r, i) => (
				<div key={i}>
					<div className={cn(FLOW_GRID, "text-brand text-sm")}>
						<span className="min-w-0 justify-self-center font-semibold">
							{r.label}:
						</span>
						<span className="min-w-0 justify-self-center">{r.amount}</span>
						<span className="max-w-full min-w-0 justify-self-center truncate font-mono text-xs">
							{r.address}
						</span>
					</div>
					{i < rows.length - 1 && <ArrowsRow direction={direction} />}
				</div>
			))}
		</div>
	);
}


function Row({
	color,
	label,
	amount,
	address,
}: {
	color: "online" | "offline";
	label: string;
	amount: string;
	address: string;
}) {
	const klass =
		color === "online" ? "text-status-online" : "text-status-offline";
	return (
		<div className={cn(FLOW_GRID, "text-sm", klass)}>
			<span className="min-w-0 justify-self-center font-semibold">
				{label}:
			</span>
			<span className="min-w-0 justify-self-center">{amount}</span>
			<span className="max-w-full min-w-0 justify-self-center truncate font-mono text-xs">
				{address}
			</span>
		</div>
	);
}

/* ---------- Delete Dialog ---------- */

function DeleteDialog({
	open,
	onOpenChange,
	onConfirm,
	confirmDisabled,
}: {
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onConfirm: (reason: string, notes: string) => void;
	confirmDisabled?: boolean;
}) {
	const [reason, setReason] = useState<string>("");
	const [notes, setNotes] = useState<string>("");

	const canConfirm = useMemo(() => reason.length > 0, [reason]);

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent showClose={false} className="max-w-sm">
				<DialogHeader>
					<DialogTitle>Are you sure this is not an attack?</DialogTitle>
					<DialogDescription>
						You are deleting this transaction permanently. This action is
						irreversible.
					</DialogDescription>
				</DialogHeader>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="archive-reason" className="text-xs">
						Reason
					</Label>
					<Select value={reason} onValueChange={setReason}>
						<SelectTrigger id="archive-reason" className="h-11">
							<SelectValue placeholder="Reason" />
						</SelectTrigger>
						<SelectContent>
							{ARCHIVE_REASONS.map((r) => (
								<SelectItem key={r} value={r}>
									{r}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
				</div>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="archive-notes" className="text-xs">
						Notes
					</Label>
					<Textarea
						id="archive-notes"
						placeholder="Details here"
						value={notes}
						onChange={(e) => setNotes(e.target.value)}
						rows={3}
					/>
				</div>

				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						Cancel
					</Button>
					<Button
						variant="default"
						disabled={!canConfirm || confirmDisabled}
						onClick={() => onConfirm(reason, notes)}
						className="text-brand border-border hover:bg-accent hover:text-brand border bg-transparent"
					>
						{confirmDisabled ? "Archiving…" : "Confirm"}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
