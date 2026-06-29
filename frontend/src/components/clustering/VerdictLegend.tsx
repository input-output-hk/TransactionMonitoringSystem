/**
 * Colour key for the Graph and Projection plots, which colour each transaction
 * by verdict (flagged txs pop) and otherwise by cluster. Colours are sourced
 * from the shared `verdict.ts` palette so the key can never drift from what the
 * plots actually draw.
 */
import { NOISE_COLOR, VERDICT_COLOR } from "./verdict";

// VERDICT_COLOR only defines the verdicts that override the cluster colour
// (malicious, anomaly); fall back to the noise grey if one is ever dropped, so
// the key stays cast-free and never renders an undefined swatch.
const ITEMS: { color: string; label: string }[] = [
	{
		color: VERDICT_COLOR.malicious ?? NOISE_COLOR,
		label: "malicious (flagged)",
	},
	{
		color: VERDICT_COLOR.anomaly ?? NOISE_COLOR,
		label: "anomaly (auto-detected)",
	},
	{ color: NOISE_COLOR, label: "noise" },
];

export function VerdictLegend() {
	return (
		<div className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
			{ITEMS.map((it) => (
				<span key={it.label} className="inline-flex items-center gap-1.5">
					<span
						className="inline-block h-2.5 w-2.5 rounded-full"
						style={{ background: it.color }}
					/>
					{it.label}
				</span>
			))}
			<span>other colours = cluster</span>
		</div>
	);
}
