import { useId, useRef, useState } from "react";
import type { AlertTimeseriesPoint } from "@/lib/api/stats";
import { parseUtcInstant } from "@/lib/utils/dates";

// Matches the Tailwind h-10 (2.5rem = 40px) slot the card reserves for the
// chart. Kept as a constant so the container height and the layout slot
// can't drift apart.
const SPARK_HEIGHT = 40;
// The SVG is drawn in an abstract 100-wide viewBox and stretched to the
// container width (preserveAspectRatio="none"); only the viewBox coords below
// are in these units.
const VIEW_W = 100;
// Vertical breathing room so the peak/trough aren't clipped at the edges.
const PAD_Y = 3;

const MONTHS = [
	"Jan",
	"Feb",
	"Mar",
	"Apr",
	"May",
	"Jun",
	"Jul",
	"Aug",
	"Sep",
	"Oct",
	"Nov",
	"Dec",
];

/** Deterministic UTC "Mon D" label for a date-only or ISO timestamp. Raw
 *  string on unparseable input. UTC getters (not toLocaleDateString) so the
 *  label doesn't shift with the viewer's timezone. */
function fmtDay(s: string): string {
	const d = parseUtcInstant(s.length <= 10 ? `${s}T00:00:00Z` : s);
	if (d === null) return s;
	return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/**
 * Area-chart sparkline for the dashboard alert trend, drawn as inline SVG.
 *
 * Deliberately dependency-free (previously recharts, ~280 KB gzip of d3/redux
 * transitive deps for a 40px chart): one area path plus a stroke, a per-
 * instance gradient, and a hover tooltip. Because there is no heavy chart lib
 * to defer, AttacksPage imports it statically rather than via React.lazy.
 */
export default function Sparkline({
	points,
	className,
}: {
	points: AlertTimeseriesPoint[];
	className?: string;
}) {
	// Unique gradient id per instance: SVG gradient ids are document-global, so
	// a hardcoded id would collide if more than one Sparkline mounts. Strip
	// colons — useId emits ":r3:", awkward inside a url(#...) reference.
	const gradId = `spark-${useId().replace(/:/g, "")}`;
	const svgRef = useRef<SVGSVGElement>(null);
	const [hover, setHover] = useState<number | null>(null);

	const n = points.length;
	if (n === 0) return <div className={className} style={{ height: SPARK_HEIGHT }} />;

	const counts = points.map((p) => p.count);
	const max = Math.max(...counts);
	const min = Math.min(...counts);
	const span = max - min || 1;
	// Single point: a flat line at mid-height.
	const x = (i: number) => (n === 1 ? VIEW_W / 2 : (i / (n - 1)) * VIEW_W);
	const y = (v: number) =>
		SPARK_HEIGHT - PAD_Y - ((v - min) / span) * (SPARK_HEIGHT - 2 * PAD_Y);

	const line = points.map((p, i) => `${x(i)},${y(p.count)}`).join(" ");
	const area = `M0,${SPARK_HEIGHT} L${points
		.map((p, i) => `${x(i)},${y(p.count)}`)
		.join(" L")} L${VIEW_W},${SPARK_HEIGHT} Z`;

	function onMove(e: React.MouseEvent<SVGSVGElement>) {
		const rect = svgRef.current?.getBoundingClientRect();
		if (!rect || rect.width === 0) return;
		const frac = (e.clientX - rect.left) / rect.width;
		setHover(Math.min(n - 1, Math.max(0, Math.round(frac * (n - 1)))));
	}

	return (
		<div className={className} style={{ position: "relative", height: SPARK_HEIGHT }}>
			<svg
				ref={svgRef}
				width="100%"
				height={SPARK_HEIGHT}
				viewBox={`0 0 ${VIEW_W} ${SPARK_HEIGHT}`}
				preserveAspectRatio="none"
				onMouseMove={onMove}
				onMouseLeave={() => setHover(null)}
			>
				<defs>
					<linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
						<stop offset="0%" stopColor="var(--color-brand)" stopOpacity={0.35} />
						<stop offset="100%" stopColor="var(--color-brand)" stopOpacity={0} />
					</linearGradient>
				</defs>
				<path d={area} fill={`url(#${gradId})`} />
				<polyline
					points={line}
					fill="none"
					stroke="var(--color-brand)"
					strokeWidth={1.5}
					vectorEffect="non-scaling-stroke"
				/>
			</svg>
			{hover !== null && (
				<div
					// Tooltip themed via the same popover tokens the old recharts
					// contentStyle used, so it isn't a white box in dark mode.
					className="border-border bg-popover text-popover-foreground pointer-events-none absolute top-0 -translate-x-1/2 rounded-md border px-1.5 py-0.5 text-[11px] whitespace-nowrap"
					style={{ left: `${(x(hover) / VIEW_W) * 100}%` }}
				>
					{fmtDay(points[hover].date)} · {points[hover].count.toLocaleString()} alerts
				</div>
			)}
		</div>
	);
}
