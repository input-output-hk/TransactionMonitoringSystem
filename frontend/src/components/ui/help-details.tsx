/**
 * Collapsible terminology/help disclosure for the analyst surfaces. A native
 * <details>/<summary> styled to the TMS tokens (so it reads on both themes),
 * collapsed by default so it stays out of the way until an analyst wants the
 * glossary. Replaces the old UI's `<details className="help">` blocks.
 */
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export function HelpDetails({
	summary,
	className,
	children,
}: {
	summary: string;
	className?: string;
	children: React.ReactNode;
}) {
	return (
		<details
			className={cn(
				"group border-border bg-muted/30 rounded-md border",
				className,
			)}
		>
			<summary className="text-muted-foreground hover:text-foreground flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-xs font-medium select-none">
				<ChevronRight
					aria-hidden
					className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
				/>
				{summary}
			</summary>
			<div className="text-muted-foreground [&_code]:text-foreground [&_strong]:text-foreground space-y-2 px-3 pb-3 text-xs leading-relaxed [&_li]:ml-4 [&_li]:list-disc [&_ul]:space-y-1">
				{children}
			</div>
		</details>
	);
}
