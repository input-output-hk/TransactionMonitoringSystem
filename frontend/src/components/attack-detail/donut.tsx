import { Info } from "lucide-react";

export function DonutCard({
	label,
	percent,
}: {
	label: string;
	percent: number;
}) {
	return (
		<div className="border-border bg-muted/30 relative rounded-xl border p-4">
			<button
				type="button"
				title={`${label} score detail`}
				className="text-muted-foreground/70 hover:text-foreground absolute top-3 right-3"
			>
				<Info className="h-3.5 w-3.5" />
			</button>
			<div className="text-foreground/90 text-center text-xs font-medium tracking-wide uppercase">
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
				strokeLinecap="round"
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
				%{percent}
			</text>
		</svg>
	);
}
