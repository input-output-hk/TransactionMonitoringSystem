/** Shared verdict styling for the clustering surfaces. */
import type { Verdict } from "@/lib/api/clustering";

/** Map an engine verdict onto the host's severity Badge variants, so a
 *  contract-anomaly verdict reads with the same colour language as the other
 *  attack classes (malicious ~ critical, anomaly ~ high, benign/normal muted). */
export const VERDICT_BADGE: Record<
	Verdict,
	"low" | "medium" | "high" | "critical" | "outline"
> = {
	malicious: "critical",
	anomaly: "high",
	benign: "low",
	normal: "outline",
};

export const VERDICT_LABEL: Record<Verdict, string> = {
	malicious: "Malicious",
	anomaly: "Anomaly",
	benign: "Benign",
	normal: "Normal",
};

// Cluster fill colours for the graph. Index by (cluster_id mod palette); the
// noise bucket (-1) is rendered muted. Chosen to read on both themes.
const CLUSTER_PALETTE = [
	"#3b82f6",
	"#22c55e",
	"#f59e0b",
	"#a855f7",
	"#ec4899",
	"#06b6d4",
	"#84cc16",
	"#f97316",
	"#6366f1",
	"#14b8a6",
];
const NOISE_COLOR = "#9ca3af";
// Verdict overrides the cluster colour: a flagged tx must pop regardless of
// which cluster it sits in.
const VERDICT_COLOR: Partial<Record<Verdict, string>> = {
	malicious: "#ef4444",
	anomaly: "#f59e0b",
};

/** Palette fill for a cluster id alone (no verdict override): the noise bucket
 *  (-1) is muted, otherwise indexed into the shared palette. Used for the small
 *  cluster-colour dots in the cluster/latest tables, where verdict is carried by
 *  a Badge rather than the swatch. */
export function clusterColor(cluster: number): string {
	if (cluster < 0) return NOISE_COLOR;
	return CLUSTER_PALETTE[cluster % CLUSTER_PALETTE.length];
}

export function nodeColor(cluster: number, verdict: Verdict): string {
	if (VERDICT_COLOR[verdict]) return VERDICT_COLOR[verdict] as string;
	return clusterColor(cluster);
}

/** Display name for a cluster id: the noise bucket (-1) reads "Noise". */
export function clusterLabel(id: number): string {
	return id < 0 ? "Noise" : `Cluster ${id}`;
}
