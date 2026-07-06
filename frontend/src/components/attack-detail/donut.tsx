import { Info } from "lucide-react";
import {
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";

export function DonutCard({
	label,
	percent,
	description,
}: {
	label: string;
	percent: number;
	/**
	 * Operator-facing explanation of what this sub-score measures.
	 * Rendered in a hover tooltip behind the info icon. When omitted the
	 * icon is hidden — keeps unconfigured donuts from showing a dead
	 * affordance.
	 */
	description?: string;
}) {
	return (
		<div className="border-border relative rounded-md border bg-white p-4 dark:bg-[#383838]">
			{description && (
				<Tooltip>
					<TooltipTrigger asChild>
						<button
							type="button"
							aria-label={`Info: ${label}`}
							className="text-muted-foreground/70 hover:text-foreground focus-visible:ring-ring absolute top-3 right-3 rounded focus-visible:ring-2 focus-visible:outline-none"
						>
							<Info className="h-3.5 w-3.5" />
						</button>
					</TooltipTrigger>
					<TooltipContent side="top" align="end" className="max-w-xs text-xs">
						{description}
					</TooltipContent>
				</Tooltip>
			)}
			{/* `px-7` reserves room on both sides for the absolute info icon at
			    top-right; symmetric so the label stays visually centered. */}
			<div className="text-foreground/90 px-7 text-center text-xs font-medium tracking-wide uppercase">
				{label}
			</div>
			<div className="mt-3 flex justify-center">
				<Donut percent={percent} />
			</div>
		</div>
	);
}

function Donut({ percent, size = 110 }: { percent: number; size?: number }) {
	const stroke = 10;
	const r = (size - stroke) / 2;
	const c = 2 * Math.PI * r;
	const dash = (percent / 100) * c;
	return (
		<svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
			<circle
				cx={size / 2}
				cy={size / 2}
				r={r}
				fill="none"
				className="stroke-border"
				strokeWidth={stroke}
			/>
			<circle
				cx={size / 2}
				cy={size / 2}
				r={r}
				fill="none"
				className="stroke-brand"
				strokeWidth={stroke}
				// `butt` (SVG default — making it explicit) gives the arc
				// straight squared-off ends, per Figma.
				strokeLinecap="butt"
				strokeDasharray={`${dash} ${c - dash}`}
				transform={`rotate(-90 ${size / 2} ${size / 2})`}
			/>
			<text
				x="50%"
				y="50%"
				dominantBaseline="central"
				textAnchor="middle"
				className="fill-foreground text-base font-semibold"
			>
				{percent}%
			</text>
		</svg>
	);
}
