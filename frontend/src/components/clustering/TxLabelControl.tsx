/**
 * Compact per-transaction label control (Mark malicious / benign / clear).
 *
 * A per-tx label is the highest-precedence verdict signal and overrides any
 * cluster inheritance, but unlike a cluster label it applies to THIS tx only and
 * does NOT propagate to future transactions. It is the way to judge a single tx
 * that belongs to no labelable cluster (e.g. a noise-bucket outlier). The row's
 * own badge shows the resulting verdict, so this is purely the action.
 */
import { Button } from "@/components/ui/button";
import {
	type ClusterVerdict,
	useClearTxLabel,
	useLabelTx,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";

type Props = {
	target: string;
	txHash: string;
	// The tx's OWN explicit label (null = none). Drives the controls, NOT the
	// effective verdict, so "clear" is offered only when there is an own label
	// to remove, never for a verdict merely inherited from the cluster.
	ownLabel: ClusterVerdict | null;
};

export function TxLabelControl({ target, txHash, ownLabel }: Props) {
	const { isAdmin } = useAuth();
	const label = useLabelTx();
	const clear = useClearTxLabel();
	const busy = label.isPending || clear.isPending;

	// Per-tx labelling is an Admin-only mutation at the proxy; a read-only
	// Reviewer sees no action controls (the row's verdict badge still renders).
	if (!isAdmin) return null;

	return (
		<div className="flex justify-end gap-1">
			<Button
				variant="ghost"
				size="sm"
				className="h-7 px-2 text-xs"
				disabled={busy || ownLabel === "malicious"}
				title="Mark this single transaction malicious (does not propagate to future txs)"
				onClick={() => label.mutate({ target, txHash, verdict: "malicious" })}
			>
				Mal
			</Button>
			<Button
				variant="ghost"
				size="sm"
				className="h-7 px-2 text-xs"
				disabled={busy || ownLabel === "benign"}
				title="Mark this single transaction benign (does not propagate to future txs)"
				onClick={() => label.mutate({ target, txHash, verdict: "benign" })}
			>
				Ben
			</Button>
			<Button
				variant="ghost"
				size="sm"
				className="text-muted-foreground h-7 px-2 text-xs"
				disabled={busy || ownLabel === null}
				title="Clear this transaction's own manual label"
				onClick={() => clear.mutate({ target, txHash })}
			>
				Clear
			</Button>
		</div>
	);
}
