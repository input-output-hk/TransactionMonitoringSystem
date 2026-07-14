import { useId } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import type { AlertTimeseriesPoint } from "@/lib/api/stats";

// Matches the Tailwind h-10 (2.5rem = 40px) slot the card reserves for the
// chart. Kept as a constant so the container height and the layout slot
// can't drift apart.
const SPARK_HEIGHT = 40;

/**
 * Area-chart sparkline for the dashboard alert trend.
 *
 * Isolated in its own module and consumed via React.lazy from AttacksPage so
 * recharts (and its heavy d3 / redux transitive deps, ~280 KB gzip) ships as
 * a deferred async chunk rather than bloating the initial dashboard bundle.
 */
export default function Sparkline({
	points,
	className,
}: {
	points: AlertTimeseriesPoint[];
	className?: string;
}) {
	// Unique gradient id per instance: SVG gradient ids are document-global,
	// so a hardcoded id would collide if more than one Sparkline mounts.
	// Strip colons — React's useId emits ids like ":r3:", which are awkward
	// inside an SVG `url(#...)` fragment reference.
	const gradId = `spark-${useId().replace(/:/g, "")}`;
	const fmtDay = (iso: string) => {
		const d = new Date(iso);
		return Number.isNaN(d.getTime())
			? iso
			: d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
	};
	// Fixed numeric height (matches the h-10 = 40px slot) rather than
	// height="100%": ResponsiveContainer can't resolve a percentage height
	// before the parent has laid out, which logs a width(-1)/height(-1)
	// warning and delays first paint. width stays responsive; minWidth={0}
	// lets it shrink without warning inside the flex/grid cell.
	return (
		<div className={className}>
			<ResponsiveContainer width="100%" height={SPARK_HEIGHT} minWidth={0}>
				<AreaChart data={points} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
					<defs>
						<linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
							<stop offset="0%" stopColor="var(--color-brand)" stopOpacity={0.35} />
							<stop offset="100%" stopColor="var(--color-brand)" stopOpacity={0} />
						</linearGradient>
					</defs>
					{/* Hidden axis so the tooltip label resolves to the date
					    rather than the array index. */}
					<XAxis dataKey="date" hide />
					<Tooltip
						cursor={false}
						contentStyle={{
							fontSize: "11px",
							padding: "2px 6px",
							borderRadius: "6px",
							// Theme via CSS tokens so the tooltip isn't a white box
							// in dark mode (recharts' default is light-only).
							background: "var(--color-popover)",
							border: "1px solid var(--color-border)",
							color: "var(--color-popover-foreground)",
						}}
						labelFormatter={(label) => fmtDay(String(label))}
						formatter={(value) => [
							typeof value === "number"
								? value.toLocaleString()
								: String(value ?? ""),
							"alerts",
						]}
					/>
					<Area
						type="monotone"
						dataKey="count"
						stroke="var(--color-brand)"
						strokeWidth={1.5}
						fill={`url(#${gradId})`}
						isAnimationActive={false}
						dot={false}
					/>
				</AreaChart>
			</ResponsiveContainer>
		</div>
	);
}
