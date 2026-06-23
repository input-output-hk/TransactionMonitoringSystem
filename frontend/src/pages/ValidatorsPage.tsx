/**
 * Watched Validators: manage the contracts the clustering sidecar monitors.
 *
 * Mirrors the engine's Validators page UX (add a contract, see its status /
 * drift, open its clusters), reskinned to the TMS design system. There is no
 * "fetch from Blockfrost" action — the sidecar auto-feeds each watched contract
 * from the host's chain data. Gated on the clustering module being enabled.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardFooter,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
	type Contract,
	useAddContract,
	useClassifyNow,
	useContracts,
	useDeleteContract,
} from "@/lib/api/clustering";
import { useHealth } from "@/lib/api/health";

function statusVariant(status: string): "low" | "medium" | "high" | "critical" | "outline" {
	if (status === "done") return "low";
	if (status === "failed") return "critical";
	if (status === "insufficient_history") return "medium";
	return "outline"; // pending / processing
}

function shortTarget(t: string): string {
	return t.length > 24 ? `${t.slice(0, 16)}…${t.slice(-6)}` : t;
}

function ContractCard({ c }: { c: Contract }) {
	const navigate = useNavigate();
	const del = useDeleteContract();
	const classify = useClassifyNow();
	return (
		<Card>
			<CardHeader>
				<CardTitle className="flex items-center justify-between gap-2">
					<span className="truncate font-mono text-sm" title={c.target}>
						{c.label || shortTarget(c.target)}
					</span>
					<Badge variant={statusVariant(c.status)}>{c.status}</Badge>
				</CardTitle>
			</CardHeader>
			<CardContent className="space-y-1 text-sm text-muted-foreground">
				<div className="flex justify-between">
					<span>Transactions</span>
					<span className="tabular-nums text-foreground">
						{c.tx_count.toLocaleString()}
					</span>
				</div>
				<div className="flex justify-between">
					<span>Drift</span>
					<span className="tabular-nums text-foreground">
						{(c.drift_score * 100).toFixed(0)}%
					</span>
				</div>
				{c.reclustering_suggested && (
					<Badge variant="medium">re-cluster suggested</Badge>
				)}
			</CardContent>
			<CardFooter className="gap-2">
				<Button
					size="sm"
					onClick={() => navigate(`/validators/${encodeURIComponent(c.target)}`)}
				>
					Open
				</Button>
				<Button
					variant="outline"
					size="sm"
					disabled={classify.isPending}
					onClick={() => classify.mutate(c.target)}
				>
					Refresh
				</Button>
				<Button
					variant="ghost"
					size="sm"
					disabled={del.isPending}
					onClick={() => {
						if (confirm(`Stop watching ${c.target}?`)) del.mutate(c.target);
					}}
				>
					Remove
				</Button>
			</CardFooter>
		</Card>
	);
}

export function ValidatorsPage() {
	const health = useHealth();
	const { data: contracts, isLoading, isError } = useContracts();
	const add = useAddContract();
	const [target, setTarget] = useState("");
	const [label, setLabel] = useState("");

	if (health.data && health.data.clustering_enabled === false) {
		return (
			<div className="text-sm text-muted-foreground">
				The clustering module is not enabled on this deployment.
			</div>
		);
	}

	const onAdd = () => {
		const t = target.trim();
		if (!t) return;
		add.mutate(
			{ target: t, label: label.trim() || undefined },
			{ onSuccess: () => { setTarget(""); setLabel(""); } },
		);
	};

	return (
		<div className="space-y-6">
			<div>
				<h1 className="text-xl font-semibold">Watched Validators</h1>
				<p className="text-sm text-muted-foreground">
					Contracts monitored by the clustering engine. New transactions are
					classified automatically as the chain is ingested; anomalies surface
					as the <span className="font-medium">Contract Anomaly</span> attack type.
				</p>
			</div>

			<Card>
				<CardHeader>
					<CardTitle>Add a contract</CardTitle>
				</CardHeader>
				<CardContent className="flex flex-wrap items-end gap-2">
					<div className="flex-1 min-w-64">
						<Input
							placeholder="Script address (addr1… / addr_test1…)"
							value={target}
							onChange={(e) => setTarget(e.target.value)}
						/>
					</div>
					<div className="w-48">
						<Input
							placeholder="Label (optional)"
							value={label}
							onChange={(e) => setLabel(e.target.value)}
						/>
					</div>
					<Button onClick={onAdd} disabled={add.isPending || !target.trim()}>
						{add.isPending ? "Adding…" : "Add"}
					</Button>
				</CardContent>
				{add.isError && (
					<CardFooter>
						<span className="text-sm text-destructive">
							Could not add the contract. Check the address and try again.
						</span>
					</CardFooter>
				)}
			</Card>

			{isLoading ? (
				<p className="text-sm text-muted-foreground">Loading watchlist…</p>
			) : isError ? (
				<p className="text-sm text-destructive">
					Could not load the watchlist. The clustering service may be
					unavailable; retry shortly.
				</p>
			) : !contracts?.length ? (
				<p className="text-sm text-muted-foreground">
					No contracts watched yet. Add a script address above to start.
				</p>
			) : (
				<div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
					{contracts.map((c) => (
						<ContractCard key={c.target} c={c} />
					))}
				</div>
			)}
		</div>
	);
}
