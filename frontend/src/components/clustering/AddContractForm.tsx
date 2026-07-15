/**
 * The "Add a contract" onboarding form for the Watched Validators page. It
 * identifies a typed target against the bundled registry (debounced) and
 * prefills its display name, then onboards (or re-analyzes) it. Self-contained:
 * owns its input state and drives `useAddContract`.
 */
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	MAX_TXS_CAP,
	useAddContract,
	useClusteringConfig,
	useIdentify,
} from "@/lib/api/clustering";

// Default onboarding window: import the most recent 500 txs unless the analyst
// asks for more (0 = all history). Keeps first onboarding fast.
const DEFAULT_MAX_TXS = 500;

// Debounce before identifying a typed target, so we don't hit the registry on
// every keystroke while the analyst is still pasting an address.
const IDENTIFY_DEBOUNCE_MS = 350;

/** Debounce a changing value; the timeout fires the update, so this never sets
 *  state synchronously in an effect body. */
function useDebounced<T>(value: T, ms: number): T {
	const [debounced, setDebounced] = useState(value);
	useEffect(() => {
		const h = setTimeout(() => setDebounced(value), ms);
		return () => clearTimeout(h);
	}, [value, ms]);
	return debounced;
}

// `enabled` mirrors the page's clustering-enabled gate so the config fetch (like
// the watchlist/jobs polls) never hits /api/v1/clustering/* until health confirms
// the module is on.
export function AddContractForm({ enabled = true }: { enabled?: boolean }) {
	const add = useAddContract();
	const config = useClusteringConfig(enabled);
	// On a host-backed deployment the engine reads txs from the host tables and
	// fits over the global rolling window, so a per-contract "max txs" cap does
	// nothing — hide the control rather than silently ignore it. Until config
	// loads (or if it errors) we treat host-backed as unknown: the cap control
	// stays hidden and no max_txs is sent, rather than guessing.
	const hostBacked = config.data?.host_backed ?? false;
	const [target, setTarget] = useState("");
	const [nameDraft, setNameDraft] = useState("");
	const [nameTouched, setNameTouched] = useState(false);
	const [maxTxs, setMaxTxs] = useState(DEFAULT_MAX_TXS);
	const [reprocess, setReprocess] = useState(false);

	const debouncedTarget = useDebounced(target, IDENTIFY_DEBOUNCE_MS);
	const identify = useIdentify(debouncedTarget);
	const recog = identify.data;

	// Display name: the registry label until the analyst types their own. Derived
	// (not effect-synced) so a late identify response can't clobber typed input.
	const displayName = nameTouched ? nameDraft : (recog?.label ?? "");

	const onAdd = () => {
		const t = target.trim();
		if (!t) return;
		add.mutate(
			{
				target: t,
				label: displayName.trim() || undefined,
				// 0 means "all history": omit max_txs so the backend onboards
				// unbounded; otherwise clamp to the API cap. Only send it once
				// config confirms a non-host-backed source — host-backed ignores
				// max_txs, and an unknown (loading/errored) config sends nothing.
				...(config.data && !hostBacked && maxTxs > 0
					? { max_txs: Math.min(maxTxs, MAX_TXS_CAP) }
					: {}),
				...(reprocess ? { reprocess: true } : {}),
			},
			{
				onSuccess: () => {
					setTarget("");
					setNameDraft("");
					setNameTouched(false);
					setMaxTxs(DEFAULT_MAX_TXS);
					setReprocess(false);
				},
			},
		);
	};

	return (
		<Card>
			<CardHeader>
				<CardTitle className="text-base">Add a contract</CardTitle>
			</CardHeader>
			<CardContent className="space-y-3">
				<div className="space-y-1.5">
					<Label htmlFor="add-target">Script address</Label>
					<Input
						id="add-target"
						placeholder="addr1… / addr_test1…"
						value={target}
						onChange={(e) => setTarget(e.target.value)}
					/>
					{recog?.valid &&
						(recog.label ? (
							<p className="text-muted-foreground text-xs">
								Recognized: <span className="font-medium">{recog.label}</span> ·
								source: StricaHQ registry
							</p>
						) : (
							<p className="text-muted-foreground text-xs">
								Not in the StricaHQ registry — add your own name below
								(optional).
							</p>
						))}
				</div>

				<div className="flex flex-wrap items-end gap-3">
					<div className="min-w-56 flex-1 space-y-1.5">
						<Label htmlFor="add-name">Display name (optional)</Label>
						<Input
							id="add-name"
							placeholder="e.g. Minswap Order Contract"
							value={displayName}
							onChange={(e) => {
								setNameTouched(true);
								setNameDraft(e.target.value);
							}}
						/>
					</div>
					{config.data && !hostBacked && (
						<div className="w-40 space-y-1.5">
							<Label htmlFor="add-max-txs">
								Max txs {maxTxs === 0 ? "(0 = all)" : ""}
							</Label>
							<Input
								id="add-max-txs"
								type="number"
								min={0}
								max={MAX_TXS_CAP}
								value={maxTxs}
								disabled={reprocess}
								onChange={(e) =>
									setMaxTxs(Math.max(0, Number(e.target.value) || 0))
								}
							/>
						</div>
					)}
					<label className="text-muted-foreground flex h-11 items-center gap-1.5 text-sm select-none">
						<input
							type="checkbox"
							className="accent-primary h-4 w-4"
							checked={reprocess}
							onChange={(e) => setReprocess(e.target.checked)}
						/>
						Reprocess only
					</label>
					<Button onClick={onAdd} disabled={add.isPending || !target.trim()}>
						{add.isPending ? "Adding…" : reprocess ? "Re-analyze" : "Onboard"}
					</Button>
				</div>
				{config.data && (
					<p className="text-muted-foreground text-xs">
						{hostBacked ? (
							<>
								Clusters and anomaly-scores this contract over its most recent{" "}
								{config.data.window_txs.toLocaleString()} transactions already
								monitored on-chain (the rolling fit window). Onboarding runs in
								the background; the card below tracks progress.
							</>
						) : (
							<>
								Downloads the most recent N transactions (0 = all history), then
								clusters and anomaly-scores them. Onboarding runs in the
								background; the card below tracks progress.
							</>
						)}
					</p>
				)}
				{add.isError && (
					<p className="text-destructive text-sm">
						Could not add the contract. Check the address and try again.
					</p>
				)}
			</CardContent>
		</Card>
	);
}
