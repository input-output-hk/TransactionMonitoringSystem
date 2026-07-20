/**
 * One watched-contract card for the Validators page: identity + inline rename,
 * live job status, and the management actions (explore, outliers, fetch recent,
 * re-analyze, download more, delete). The dialogs and helpers here are private
 * to the card.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
	Download,
	MoreVertical,
	Pencil,
	RefreshCw,
	Trash2,
} from "lucide-react";

import { JobProgress } from "@/components/clustering/JobProgress";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardFooter,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	type Contract,
	type Job,
	MAX_TXS_CAP,
	isTerminalJob,
	useAddContract,
	useClassifyNow,
	useDeleteContract,
	useRenameContract,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { copyToClipboard } from "@/lib/utils/clipboard";

function statusVariant(
	status: string,
): "low" | "medium" | "high" | "critical" | "outline" {
	if (status === "done") return "low";
	if (status === "failed") return "critical";
	if (status === "insufficient_history") return "medium";
	return "outline"; // pending / processing
}

function shortTarget(t: string): string {
	return t.length > 24 ? `${t.slice(0, 16)}…${t.slice(-6)}` : t;
}

// Pre-filled count in the "Download more" dialog: a sensible chunk to extend the
// window by, clamped to the remaining room under MAX_TXS_CAP at submit time.
const DEFAULT_DOWNLOAD_MORE = 1000;

/** Inline rename control for a contract's display name. */
function RenameField({
	contract,
	onDone,
}: {
	contract: Contract;
	onDone: () => void;
}) {
	const rename = useRenameContract();
	const [draft, setDraft] = useState(contract.label);

	const save = () =>
		rename.mutate(
			{ target: contract.target, label: draft.trim() },
			{ onSuccess: onDone },
		);

	return (
		<div className="flex items-center gap-1">
			<Input
				className="h-8"
				autoFocus
				value={draft}
				placeholder="Display name"
				disabled={rename.isPending}
				onChange={(e) => setDraft(e.target.value)}
				onKeyDown={(e) => {
					if (e.key === "Enter") save();
					if (e.key === "Escape") onDone();
				}}
			/>
			<Button
				variant="ghost"
				size="sm"
				disabled={rename.isPending}
				onClick={save}
			>
				Save
			</Button>
			<Button variant="ghost" size="sm" onClick={onDone}>
				Cancel
			</Button>
		</div>
	);
}

function DownloadMoreDialog({
	contract,
	open,
	onOpenChange,
}: {
	contract: Contract;
	open: boolean;
	onOpenChange: (v: boolean) => void;
}) {
	const add = useAddContract();
	const [count, setCount] = useState(DEFAULT_DOWNLOAD_MORE);
	// Room left under the cap, and the resulting total to request (clamped) so it
	// never exceeds MAX_TXS_CAP.
	const room = Math.max(0, MAX_TXS_CAP - contract.tx_count);
	const n = Math.min(Math.max(1, Math.trunc(count)), Math.max(1, room));
	const newTotal = Math.min(MAX_TXS_CAP, contract.tx_count + n);

	const submit = () =>
		add.mutate(
			{ target: contract.target, max_txs: newTotal },
			{ onSuccess: () => onOpenChange(false) },
		);

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>Download more transactions</DialogTitle>
					<DialogDescription>
						Extends the analysis window and re-clusters + re-scores everything.
						Name and labels are kept. Capped so the total stays within{" "}
						{MAX_TXS_CAP.toLocaleString()}.
					</DialogDescription>
				</DialogHeader>
				<div className="space-y-1.5">
					<Label htmlFor="dl-more">How many more transactions to add</Label>
					<Input
						id="dl-more"
						type="number"
						min={1}
						max={room}
						value={n}
						autoFocus
						onChange={(e) => setCount(Math.trunc(Number(e.target.value) || 0))}
					/>
				</div>
				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						Cancel
					</Button>
					<Button disabled={add.isPending || room <= 0} onClick={submit}>
						{add.isPending
							? "Starting…"
							: `Download ${n.toLocaleString()} more`}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

function DeleteDialog({
	contract,
	open,
	onOpenChange,
	onDeleted,
}: {
	contract: Contract;
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onDeleted: () => void;
}) {
	const del = useDeleteContract();
	const [typed, setTyped] = useState("");
	// Confirm against the label when set (the display name), else the raw target.
	const confirmValue = contract.label || contract.target;

	const submit = () =>
		del.mutate(contract.target, {
			onSuccess: () => {
				onOpenChange(false);
				onDeleted();
			},
		});

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>Stop watching this contract?</DialogTitle>
					<DialogDescription>
						Permanently deletes its {contract.tx_count.toLocaleString()}{" "}
						transactions, runs and scores. This cannot be undone.
					</DialogDescription>
				</DialogHeader>
				<div className="space-y-1.5">
					<Label htmlFor="del-confirm">
						Type the {contract.label ? "name" : "address"} to confirm
					</Label>
					<Input
						id="del-confirm"
						value={typed}
						autoFocus
						placeholder={confirmValue}
						onChange={(e) => setTyped(e.target.value)}
					/>
				</div>
				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						Cancel
					</Button>
					<Button
						variant="destructive"
						disabled={del.isPending || typed.trim() !== confirmValue}
						onClick={submit}
					>
						{del.isPending ? "Deleting…" : "Delete"}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

export function ContractCard({ c, job }: { c: Contract; job: Job | null }) {
	const navigate = useNavigate();
	const { isAdmin } = useAuth();
	const add = useAddContract();
	const classify = useClassifyNow();
	const [renaming, setRenaming] = useState(false);
	const [downloadOpen, setDownloadOpen] = useState(false);
	const [deleteOpen, setDeleteOpen] = useState(false);

	const jobRunning = job !== null && !isTerminalJob(job.status);
	const jobFailed = job?.status === "failed";
	const busy = jobRunning || add.isPending || classify.isPending;
	// While a job runs the pill tracks its live stage; otherwise the stored status.
	const pillVariant = jobRunning ? "outline" : statusVariant(c.status);
	const pillText = jobRunning && job ? job.status : c.status;
	const open = (tab?: string) =>
		navigate(
			`/validators/${encodeURIComponent(c.target)}${tab ? `?tab=${tab}` : ""}`,
		);

	return (
		<Card>
			<CardHeader>
				<CardTitle className="flex items-center justify-between gap-2 text-base">
					{renaming ? (
						<RenameField contract={c} onDone={() => setRenaming(false)} />
					) : (
						<span className="flex min-w-0 items-center gap-1.5">
							<span className="truncate font-mono text-sm" title={c.target}>
								{c.label || shortTarget(c.target)}
							</span>
							{isAdmin && (
								<button
									type="button"
									className="text-muted-foreground hover:text-foreground shrink-0"
									title="Rename"
									onClick={() => setRenaming(true)}
								>
									<Pencil className="h-3.5 w-3.5" />
								</button>
							)}
						</span>
					)}
					<Badge variant={pillVariant}>{pillText}</Badge>
				</CardTitle>
			</CardHeader>
			<CardContent className="text-muted-foreground space-y-1 text-sm">
				{/* Full address, always visible (the title may show a label or a
				    truncated form); break-all so the whole bech32 string wraps, and
				    click to copy. */}
				<button
					type="button"
					className="text-muted-foreground hover:text-foreground block w-full text-left font-mono text-xs break-all"
					title="Click to copy"
					onClick={() => void copyToClipboard(c.target)}
				>
					{c.target}
				</button>
				<div className="flex justify-between">
					<span>Transactions</span>
					<span className="text-foreground tabular-nums">
						{c.tx_count.toLocaleString()}
					</span>
				</div>
				<div className="flex justify-between">
					<span>Drift</span>
					<span className="text-foreground tabular-nums">
						{(c.drift_score * 100).toFixed(0)}%
					</span>
				</div>
				{c.reclustering_suggested && !jobRunning && (
					<p
						className="text-severity-medium-foreground text-xs"
						title="Recent transactions no longer fit the frozen clusters. Re-analyze to re-cluster on current data."
					>
						⚠ Model drift high — re-analyze to re-cluster.
					</p>
				)}
				<JobProgress job={job} />
			</CardContent>
			<CardFooter className="gap-2">
				<Button size="sm" onClick={() => void open()}>
					Explore
				</Button>
				<Button
					size="sm"
					variant="outline"
					onClick={() => void open("anomalies")}
				>
					Outliers
				</Button>
				{/* Every action in this menu is a clustering mutation (Admin-only at
				    the proxy), so a read-only Reviewer sees no menu rather than an
				    all-disabled one. */}
				{isAdmin && (
					<DropdownMenu>
						<DropdownMenuTrigger asChild>
							<Button size="sm" variant="ghost" aria-label="More actions">
								<MoreVertical className="h-4 w-4" />
							</Button>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end">
							<DropdownMenuItem
								disabled={busy}
								onClick={() => classify.mutate(c.target)}
							>
								<RefreshCw className="h-4 w-4" /> Fetch recent
							</DropdownMenuItem>
							<DropdownMenuItem
								disabled={busy}
								onClick={() =>
									add.mutate({ target: c.target, reprocess: true })
								}
							>
								<RefreshCw className="h-4 w-4" />
								{jobFailed ? "Retry / re-analyze" : "Re-analyze"}
							</DropdownMenuItem>
							<DropdownMenuItem
								disabled={busy || c.tx_count >= MAX_TXS_CAP}
								onClick={() => setDownloadOpen(true)}
							>
								<Download className="h-4 w-4" /> Download more…
							</DropdownMenuItem>
							<DropdownMenuSeparator />
							<DropdownMenuItem
								className="text-destructive focus:text-destructive"
								onClick={() => setDeleteOpen(true)}
							>
								<Trash2 className="h-4 w-4" /> Delete…
							</DropdownMenuItem>
						</DropdownMenuContent>
					</DropdownMenu>
				)}
			</CardFooter>

			<DownloadMoreDialog
				contract={c}
				open={downloadOpen}
				onOpenChange={setDownloadOpen}
			/>
			<DeleteDialog
				contract={c}
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				onDeleted={() => {}}
			/>
		</Card>
	);
}
