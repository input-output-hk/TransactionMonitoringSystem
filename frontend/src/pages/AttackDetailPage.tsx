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
import {
	archiveAlert,
	getArchiveMeta,
	isArchived,
	restoreAlert,
	useArchiveSnapshot,
	type ArchiveMeta,
} from "@/lib/archive-store";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import type { AttackType, RiskAlert } from "@/mocks/attacks";
import { ARCHIVE_REASONS, ATTACK_META, SUB_SCORE_LABELS } from "@/mocks/attacks";
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
	// Subscribe to snapshot so the alert + meta re-render on archive changes.
	useArchiveSnapshot();
	const { data: alert, isPending, isError } = useRiskAlert(id);
	const archivedHere = id ? isArchived(id) : false;

	if (isPending) {
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
	const archiveMeta = archived ? getArchiveMeta(alert.slug) : undefined;

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
			<AttackTypeSection type={alert.attackType} />

			<Divider />

			{/* Sub-scores */}
			<Section title="Sub-scores">
				<SubScores alert={alert} />
			</Section>

			{!archived && (
				<DeleteDialog
					open={deleteOpen}
					onOpenChange={setDeleteOpen}
					onConfirm={(reason, notes) => {
						archiveAlert(alert.slug, reason, notes || undefined);
						setDeleteOpen(false);
						onArchived();
					}}
				/>
			)}
			{archived && (
				<RestoreDialog
					open={restoreOpen}
					onOpenChange={setRestoreOpen}
					onConfirm={() => {
						restoreAlert(alert.slug);
						setRestoreOpen(false);
						onRestored();
					}}
				/>
			)}
		</section>
	);
}

function ArchiveReasonRow({ meta }: { meta: ArchiveMeta }) {
	const summary = meta.notes ? `${meta.reason}. ${meta.notes}` : meta.reason;
	return (
		<div className="flex items-baseline gap-6 px-5 py-3">
			<span className="text-brand text-sm font-semibold">
				Archive Reason & Notes:
			</span>
			<Tooltip>
				<TooltipTrigger asChild>
					<span className="text-brand min-w-0 flex-1 cursor-help truncate text-right text-sm">
						{summary}
					</span>
				</TooltipTrigger>
				<TooltipContent side="bottom" align="end" className="max-w-md">
					<div className="space-y-1">
						<div className="text-foreground font-semibold">{meta.reason}</div>
						{meta.notes && (
							<div className="text-muted-foreground whitespace-pre-line">
								{meta.notes}
							</div>
						)}
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
}: {
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onConfirm: () => void;
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
						className="border-border text-brand hover:bg-accent hover:text-brand border bg-transparent"
					>
						Confirm
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
		return { label: entry.label, percent };
	});
	return (
		<div
			className={cn(
				"grid gap-3",
				cards.length === 5 ? "md:grid-cols-5" : "md:grid-cols-4",
			)}
		>
			{cards.map((c) => (
				<DonutCard key={c.label} label={c.label} percent={c.percent} />
			))}
		</div>
	);
}

function AttackTypeSection({ type }: { type: AttackType }) {
	switch (type) {
		case "Phishing":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Extracted URLs">
								<UrlRow
									variant="warn"
									label="SUSPICIOUS"
									url="https://randomurergtrgtrgrtgrl123............45.it"
									meta="Domain Age: 3 days"
								/>
								<Divider />
								<UrlRow
									variant="danger"
									label="BLACK LISTED"
									url="https://randomurergtrgtrgrtgrl123............45.it"
									meta="Domain Age: 1 day"
								/>
								<UrlRow
									url="https://randomurergtrgtrgrtgrl123............45.it"
									meta="Domain Age: 1 day"
								/>
							</Stack>
						}
						right={
							<Stack title="Delivery Analysis">
								<KeyVal label="SEVERITY" value="SUSPICIOUS_NEW_DOMAIN" />
								<KeyVal label="SE TIER" value="Tier 1: Credential harvesting" />
								<KeyVal label="RECIPIENTS" value="47" />
								<KeyVal label="METADATA LABEL" value="CIP-20 (label 674)" />
							</Stack>
						}
					/>
				</Section>
			);

		case "Fake Token":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Token Comparison">
								<KeyVal
									label="FAKE TOKEN"
									value={
										<span className="flex flex-wrap items-center gap-3">
											<span>Age: 2 hours</span>
											<span className="font-mono text-sm">
												a1b2c3d4e5f60718...6e7f80a1
											</span>
										</span>
									}
								/>
								<KeyVal
									label="REAL TOKEN"
									value={
										<span className="flex flex-wrap items-center gap-3">
											<span>Established</span>
											<span className="font-mono text-sm">
												fcfca39395459c17...3cc7a3ce
											</span>
										</span>
									}
								/>
							</Stack>
						}
						right={
							<Stack title="Distribution">
								<KeyVal label="CIP-25 METADATA MATCH" value="72%" />
								<KeyVal label="RECIPIENTS" value="24" />
							</Stack>
						}
					/>
					<div className="mt-6">
						<Stack title="Unicode Analysis">
							<div className="grid gap-3 md:grid-cols-2">
								<UnicodeWarning text="Cyrillic О (O) replacing Latin O" />
								<UnicodeWarning text="Cyrillic У (Y) replacing Latin Y" />
							</div>
						</Stack>
					</div>
				</Section>
			);

		case "Circular":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Circular Transfer">
								<FlowChain
									direction="down"
									rows={[
										{
											label: "HOP 1",
											amount: "1,250 ADA",
											address: "a1b2c3d4e5f60718...6e7f80a1",
										},
										{
											label: "HOP 2",
											amount: "340 ADA",
											address: "fcfca39395459c17...3cc7a3ce",
										},
										{
											label: "HOP 3",
											amount: "1,287 ADA",
											address: "b2c3d4e5f6071829...7f80a1b2",
										},
									]}
								/>
							</Stack>
						}
						right={
							<Stack title="Cycle Metrics">
								<KeyVal label="AMOUNT SIMILARITY" value="96%" />
								<KeyVal label="NET LOSS" value="0.49 ADA (14%)" />
								<KeyVal label="TIMING" value="Same block (slot 118,670,944)" />
								<KeyVal label="CYCLE LENGTH" value="3 HOPS" />
							</Stack>
						}
					/>
				</Section>
			);

		case "Sandwich":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Sandwich Attack Flow">
								<SandwichFlow />
							</Stack>
						}
						right={
							<Stack title="Attack Details">
								<KeyVal label="DEX POOL" value="SundaeSwap ADA/HOSKY" />
								<KeyVal label="ASSET PAIR" value="ADA / HOSKY" />
								<KeyVal label="RATE IMPACT" value="-4.2%" />
								<KeyVal label="ATTACKER PROFIT" value="37 ADA" />
								<KeyVal label="SLOT SPAN" value="3 SLOTS" />
							</Stack>
						}
					/>
				</Section>
			);

		case "Front Running":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Collision Details">
								<KeyVal
									label={<span className="text-status-online">WINNER</span>}
									value={
										<span className="text-status-online font-mono text-sm">
											f6a7b8c9d0e1f2a3...7081923c
										</span>
									}
								/>
								<KeyVal
									label={<span className="text-status-online">Winner Fee</span>}
									value={<span className="text-status-online">0.34 ADA</span>}
								/>
								<div className="flex justify-start py-1 pl-1">
									<ArrowDown className="text-brand h-4 w-4" />
								</div>
								<KeyVal
									label={<span className="text-status-offline">LOOSER</span>}
									value={
										<span className="text-status-offline font-mono text-sm">
											c8d9e0f1a2b3c415...9a3b4c5d
										</span>
									}
								/>
								<KeyVal
									label={
										<span className="text-status-offline">Looser Fee</span>
									}
									value={<span className="text-status-offline">0.22 ADA</span>}
								/>
							</Stack>
						}
						right={
							<Stack title="Race Metrics">
								<KeyVal label="SHARED INPUTS" value="1,450 ADA" />
								<KeyVal label="MEMPOOL DELTA" value="180 ms" />
								<KeyVal label="OUTCOME" value="TX_B_CONFIRMED (Attacker Won)" />
								<KeyVal label="ATTACKER WINS" value="4 (Last 24 hours)" />
							</Stack>
						}
					/>
				</Section>
			);

		case "Multiple Sat":
			return (
				<Section>
					<TwoCol
						left={
							<Stack title="Exploit Pattern">
								<KeyVal label="SCRIPT INPUTS" value="3" />
								<KeyVal label="FULL DRAIN" value="Yes" />
								<KeyVal label="REDEEMERS USED" value="1" />
								<KeyVal label="REDEEMER RATIO" value="%33" />
							</Stack>
						}
						right={
							<Stack title="Value Flow">
								<KeyVal label="VALUE EXTRACTED" value="1,450 ADA" />
								<KeyVal label="VALUE RETURNED" value="0 ADA" />
								<KeyVal label="CPU UNITS" value="2,100,000" />
								<KeyVal
									label="TARGET SCRIPT"
									value={
										<span className="font-mono text-sm">
											ADWED34.........8678687TYHRE
										</span>
									}
								/>
							</Stack>
						}
					/>
				</Section>
			);

		case "Large Datum":
			return (
				<Section>
					<Stack title="Large Datum Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="DATUM SIZE" value="5,800 bytes" />
									<Divider />
									<KeyVal label="UTXO SIZE" value="10,000 bytes" />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal label="DATUM TYPE" value="Inline" />
									<Divider />
									<KeyVal
										label="TARGET SCRIPT"
										value={
											<span className="font-mono text-sm">
												ADWED34.........8678687TYHRE
											</span>
										}
									/>
								</div>
							}
						/>
					</Stack>
					<div className="mt-6">
						<Stack title="DATUM/UTXO Ratio">
							<RatioBar
								parts={[
									{ label: "%65 DATUM", percent: 65, tone: "brand" },
									{ label: "%35 UTXO", percent: 35, tone: "muted" },
								]}
							/>
						</Stack>
					</div>
				</Section>
			);

		case "Token Dust":
			return (
				<Section>
					<Stack title="Dust Attack Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="UNIQUE ASSETS" value="87" />
									<Divider />
									<KeyVal label="CBOR SIZE" value="14,200 bytes" />
									<Divider />
									<KeyVal label="POLICY IDS" value="12" />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal
										label="MAX QUANTITY"
										value="9,223,372,036,854,775,807"
									/>
									<Divider />
									<KeyVal
										label="TARGET SCRIPT"
										value={
											<span className="font-mono text-sm">
												ADWED34.........8678687TYHRE
											</span>
										}
									/>
									<Divider />
									<KeyVal label="ADA AMOUNT" value="1.8 ADA" />
								</div>
							}
						/>
					</Stack>
				</Section>
			);

		case "Large Value":
			return (
				<Section>
					<Stack title="Large Value Details">
						<TwoCol
							left={
								<div className="space-y-3">
									<KeyVal label="QUANTITY DIGITS" value="19" />
									<Divider />
									<KeyVal label="CBOR SIZE" value="8,400 bytes" />
									<Divider />
									<KeyVal label="ASSET NAME" value="Test1234567" />
								</div>
							}
							right={
								<div className="space-y-3">
									<KeyVal
										label="MAX QUANTITY"
										value="9,223,372,036,854,775,807"
									/>
									<Divider />
									<KeyVal
										label="POLICY ID"
										value={
											<span className="font-mono text-sm">
												ADWED34.........8678687TYHRE
											</span>
										}
									/>
									<Divider />
									<KeyVal label="ADA AMOUNT" value="2.0 ADA" />
								</div>
							}
						/>
					</Stack>
				</Section>
			);
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

function SandwichFlow() {
	return (
		<div className="space-y-1">
			<Row
				color="online"
				label="FRONT RUN"
				amount="1,250 ADA"
				address="a1b2c3d4e5f60718...6e7f80a1"
			/>
			<ArrowsRow direction="down" />
			<Row
				color="offline"
				label="VICTIM"
				amount="340 ADA"
				address="fcfca39395459c17...3cc7a3ce"
			/>
			<ArrowsRow direction="up" />
			<Row
				color="online"
				label="BACK RUN"
				amount="1,287 ADA"
				address="b2c3d4e5f6071829...7f80a1b2"
			/>
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
}: {
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onConfirm: (reason: string, notes: string) => void;
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
						disabled={!canConfirm}
						onClick={() => onConfirm(reason, notes)}
						className="text-brand border-border hover:bg-accent hover:text-brand border bg-transparent"
					>
						Confirm
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
