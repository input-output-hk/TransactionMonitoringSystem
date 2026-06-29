/**
 * Lightweight inline-SVG line chart of the sorted k-distance curve, with the
 * detected knee (suggested eps) marked. The "elbow" is where eps should sit.
 * No charting dependency: it is a handful of points.
 */
import type { Evaluation } from "@/lib/api/clustering";

type Props = { evaluation: Evaluation; width?: number; height?: number };

export function KDistanceChart({
	evaluation,
	width = 360,
	height = 150,
}: Props) {
	const dist = evaluation.k_distance.distances;
	const knee = evaluation.k_distance.knee_eps;
	if (dist.length < 2) {
		return (
			<p className="text-muted-foreground text-sm">
				Not enough points for a k-distance curve.
			</p>
		);
	}

	const pad = 24;
	// `|| 1` guards the degenerate all-zero curve so `y()` can't divide by zero
	// and emit NaN coordinates (which would break the polyline).
	const maxY = Math.max(...dist) || 1;
	const n = dist.length;
	const x = (i: number) => pad + (i / (n - 1)) * (width - 2 * pad);
	const y = (v: number) => height - pad - (v / maxY) * (height - 2 * pad);

	const points = dist
		.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
		.join(" ");
	const kneeY = knee !== null ? y(knee) : null;

	const summary =
		`k-distance curve (k=${evaluation.k_distance.k}) over ${n} points` +
		(knee !== null
			? `; suggested eps ≈ ${knee.toFixed(3)} at the knee`
			: "; no clear knee");

	return (
		<svg
			width={width}
			height={height}
			role="img"
			aria-label={summary}
			className="text-muted-foreground max-w-full"
		>
			<line
				x1={pad}
				y1={height - pad}
				x2={width - pad}
				y2={height - pad}
				stroke="currentColor"
				strokeOpacity={0.4}
			/>
			<line
				x1={pad}
				y1={pad}
				x2={pad}
				y2={height - pad}
				stroke="currentColor"
				strokeOpacity={0.4}
			/>
			<polyline
				points={points}
				fill="none"
				stroke="var(--color-brand)"
				strokeWidth={1.5}
			/>
			{kneeY !== null && (
				<line
					x1={pad}
					y1={kneeY}
					x2={width - pad}
					y2={kneeY}
					stroke="var(--color-brand)"
					strokeOpacity={0.5}
					strokeDasharray="4 3"
				/>
			)}
			<text x={pad} y={pad - 8} fontSize={11} fill="currentColor">
				k-distance (k={evaluation.k_distance.k})
				{knee !== null ? ` · knee≈${knee.toFixed(3)}` : ""}
			</text>
		</svg>
	);
}
