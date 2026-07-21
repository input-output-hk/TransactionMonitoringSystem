/**
 * Persistent, non-blocking advisory shown above the clustering / anomaly actions.
 * Reminds the analyst these are advanced tools whose output needs interpretation,
 * and links to the user guide. Deliberately not a modal: the actions are safe
 * (they create a separate custom run and never change scoring), so a callout that
 * stays out of the way beats one that trains users to click through.
 */
import { AlertTriangle } from "lucide-react";

export function DocsCallout({
	href,
	children,
}: {
	href: string;
	children: React.ReactNode;
}) {
	return (
		<div
			role="note"
			className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
		>
			<AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
			<p className="leading-relaxed">
				{children}{" "}
				<a
					href={href}
					target="_blank"
					rel="noreferrer"
					className="font-medium underline underline-offset-2"
				>
					Read the guide →
				</a>
			</p>
		</div>
	);
}
