/**
 * Compact "why flagged" chips shown under a flagged transaction. Each chip is a
 * top deviating shape feature; its direction sets a glyph and a severity tone.
 * Renders nothing when there are no reasons (non-shape runs, or unflagged rows).
 */
import type { AnomalyReason } from "@/lib/api/clustering";
import {
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { reasonGlyph } from "./format";

// Severity tone per direction: an out-of-range value (high/low) reads warmer
// than a merely categorical/combination one.
const TONE: Record<string, string> = {
	high: "border-severity-high-foreground/50 text-severity-high-foreground",
	low: "border-severity-medium-foreground/50 text-severity-medium-foreground",
	unusual: "border-border text-muted-foreground",
	combo: "border-border text-muted-foreground",
};

export function ReasonChips({ reasons }: { reasons?: AnomalyReason[] }) {
	if (!reasons?.length) return null;
	return (
		<div className="mt-1 flex flex-wrap gap-1">
			{reasons.map((r) => (
				<Tooltip key={`${r.label}:${r.direction}`}>
					<TooltipTrigger asChild>
						<span
							className={cn(
								"inline-flex items-center gap-1 rounded-[3px] border px-1.5 py-0.5 text-[10px] font-medium",
								TONE[r.direction] ?? TONE.unusual,
							)}
						>
							<span aria-hidden>{reasonGlyph(r.direction)}</span>
							{r.label}
						</span>
					</TooltipTrigger>
					<TooltipContent>{r.detail}</TooltipContent>
				</Tooltip>
			))}
		</div>
	);
}
