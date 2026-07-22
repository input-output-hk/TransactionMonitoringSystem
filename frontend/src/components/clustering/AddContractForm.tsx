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
	isPermissionDenied,
	useAddContract,
	useClusteringConfig,
	useIdentify,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { AdminOnlyGate } from "./adminOnly";

// Fallback "latest N to cluster on" used only until /config loads (or on a
// not-yet-upgraded sidecar that omits default_target_txs). The authoritative
// default is the backend's clustering_default_target_txs, surfaced via config.
const DEFAULT_TARGET_TXS_FALLBACK = 5_000;

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
	const { isAdmin } = useAuth();
	const add = useAddContract();
	const config = useClusteringConfig(enabled);
	// On a plain host-backed deployment (no history source) the fit window is
	// defined entirely by the host's tip-forward data, so there is nothing to
	// size per contract: hide the "latest N" control rather than imply it does
	// something. Until config loads (or if it errors) we treat host-backed as
	// unknown: the control stays hidden and no max_txs is sent, rather than
	// guessing.
	const hostBacked = config.data?.host_backed ?? false;
	// With a secondary history source configured, the host's recent data is
	// topped up to the contract's "latest N to cluster on", so the control (and
	// the per-contract N it sets) is meaningful again.
	const historySource = config.data?.history_source ?? "";
	const showMaxTxs = !hostBacked || Boolean(historySource);
	// The default N to pre-fill: the backend's configured default, or the
	// fallback until config loads. `null` maxTxs means "follow this default";
	// any edit pins a concrete number.
	const defaultTargetTxs =
		config.data?.default_target_txs ?? DEFAULT_TARGET_TXS_FALLBACK;
	const [target, setTarget] = useState("");
	const [nameDraft, setNameDraft] = useState("");
	const [nameTouched, setNameTouched] = useState(false);
	const [maxTxs, setMaxTxs] = useState<number | null>(null);
	const effectiveMaxTxs = maxTxs ?? defaultTargetTxs;
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
				// The per-contract "latest N to cluster on", clamped to the API
				// cap. Send it only once config confirms the control is meaningful
				// (a plain host-backed source without a history source ignores it;
				// an unknown loading/errored config sends nothing). A 0 is treated
				// as "unset" and omitted, so the backend applies its default.
				...(config.data && showMaxTxs && effectiveMaxTxs > 0
					? { max_txs: Math.min(effectiveMaxTxs, MAX_TXS_CAP) }
					: {}),
				...(reprocess ? { reprocess: true } : {}),
			},
			{
				onSuccess: () => {
					setTarget("");
					setNameDraft("");
					setNameTouched(false);
					setMaxTxs(null);
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
					{config.data && showMaxTxs && (
						<div className="w-44 space-y-1.5">
							<Label htmlFor="add-max-txs">
								{hostBacked
									? "Latest txs to cluster on"
									: `Max txs ${effectiveMaxTxs === 0 ? "(0 = all)" : ""}`}
							</Label>
							<Input
								id="add-max-txs"
								type="number"
								min={0}
								max={MAX_TXS_CAP}
								value={effectiveMaxTxs}
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
					<AdminOnlyGate gated={!isAdmin}>
						<Button
							onClick={onAdd}
							disabled={!isAdmin || add.isPending || !target.trim()}
						>
							{add.isPending ? "Adding…" : reprocess ? "Re-analyze" : "Onboard"}
						</Button>
					</AdminOnlyGate>
				</div>
				{config.data && (
					<p className="text-muted-foreground text-xs">
						{hostBacked ? (
							<>
								Clusters and anomaly-scores this contract over its most recent{" "}
								{historySource ? effectiveMaxTxs.toLocaleString() : "N"}{" "}
								transactions. These come from what the host already monitors
								on-chain
								{historySource
									? `, topped up from the ${historySource} history source when the host holds fewer, so a freshly onboarded contract still fits on the latest ${effectiveMaxTxs.toLocaleString()}. Capped at ${config.data.window_txs.toLocaleString()}.`
									: ` (the most recent ${config.data.window_txs.toLocaleString()}, the rolling fit window).`}{" "}
								Onboarding runs in the background; the card below tracks
								progress.
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
						{isPermissionDenied(add.error)
							? add.error.message
							: "Could not add the contract. Check the address and try again."}
					</p>
				)}
			</CardContent>
		</Card>
	);
}
