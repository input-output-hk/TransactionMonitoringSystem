/** Small shared table cells for the clustering surfaces, extracted so the
 *  cluster/anomaly/latest tables render transactions and clusters identically. */
import { Badge } from "@/components/ui/badge";
import type { Verdict } from "@/lib/api/clustering";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { shortHash } from "@/lib/utils/strings";
import {
	VERDICT_BADGE,
	VERDICT_LABEL,
	clusterColor,
	clusterLabel,
} from "./verdict";

/** A transaction hash rendered short + monospace; click copies the full hash. */
export function CopyHash({ hash }: { hash: string }) {
	return (
		<button
			type="button"
			className="text-foreground hover:text-brand font-mono text-xs"
			title={`${hash} — click to copy`}
			onClick={() => copyToClipboard(hash)}
		>
			{shortHash(hash)}
		</button>
	);
}

/** A transaction's effective verdict as a severity badge. A null verdict (in no
 *  run / not yet online-scored) reads as a muted "Unclassified". */
export function VerdictBadge({ verdict }: { verdict: Verdict | null }) {
	if (verdict === null) {
		return (
			<Badge variant="outline" className="text-muted-foreground">
				Unclassified
			</Badge>
		);
	}
	return (
		<Badge variant={VERDICT_BADGE[verdict]}>{VERDICT_LABEL[verdict]}</Badge>
	);
}

/** A cluster's colour dot beside its label. Verdict is carried elsewhere (by a
 *  Badge), so the swatch is the plain cluster colour. */
export function ClusterTag({ clusterId }: { clusterId: number }) {
	return (
		<span className="flex items-center gap-2">
			<span
				className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
				style={{ background: clusterColor(clusterId) }}
			/>
			{clusterLabel(clusterId)}
		</span>
	);
}
