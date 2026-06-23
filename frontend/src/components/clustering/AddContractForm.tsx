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
import { MAX_TXS_CAP, useAddContract, useIdentify } from "@/lib/api/clustering";

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

export function AddContractForm() {
	const add = useAddContract();
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
				// unbounded; otherwise clamp to the API cap.
				...(maxTxs > 0 ? { max_txs: Math.min(maxTxs, MAX_TXS_CAP) } : {}),
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
					<Label htmlFor="add-target">Address or policy id</Label>
					<Input
						id="add-target"
						placeholder="addr1… / addr_test1… or 56-hex policy id"
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
				<p className="text-muted-foreground text-xs">
					Downloads the most recent N transactions (0 = all history), then
					clusters and anomaly-scores them. Onboarding runs in the background;
					the card below tracks progress.
				</p>
				{add.isError && (
					<p className="text-destructive text-sm">
						Could not add the contract. Check the address and try again.
					</p>
				)}
			</CardContent>
		</Card>
	);
}
