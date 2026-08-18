/**
 * Layout primitives for the attack-detail card.
 *
 * These were local to `pages/AttackDetailPage.tsx`. They moved here when the
 * transaction-detail panels were added: those panels are their own components,
 * and importing the primitives back out of the page would have made the import
 * graph circular. The page re-uses them from here unchanged, so the detail view
 * keeps its one-card / divided-sections look rather than growing a second idiom.
 */
import { Children, Fragment, isValidElement, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Divider() {
	return <div className="bg-border h-px" />;
}

export function Section({
	title,
	children,
}: {
	title?: string;
	children: React.ReactNode;
}) {
	return (
		<div className="px-5 py-5">
			{title && (
				<>
					<h2 className="text-foreground mb-3 text-base font-semibold">
						{title}
					</h2>
					{/* Separator under the title — matches Figma's section style. */}
					<div className="mb-4">
						<Divider />
					</div>
				</>
			)}
			{children}
		</div>
	);
}

/**
 * React.Children.toArray treats a `<>…</>` as a single child rather than
 * walking into it. We want Stack `dividers` to interleave between each
 * inner KeyVal even when the caller wrapped them in a Fragment, so flatten
 * recursively here.
 */
function flattenChildren(children: ReactNode): ReactNode[] {
	const out: ReactNode[] = [];
	Children.forEach(children, (child) => {
		if (isValidElement(child) && child.type === Fragment) {
			out.push(
				...flattenChildren((child.props as { children?: ReactNode }).children),
			);
		} else if (child !== null && child !== undefined && child !== false) {
			out.push(child);
		}
	});
	return out;
}

export function Stack({
	title,
	children,
	dividers = false,
}: {
	/** ReactNode, not string: some panels prefix the label with an icon. */
	title: React.ReactNode;
	children: React.ReactNode;
	/** Intersperse a horizontal divider between each direct child. */
	dividers?: boolean;
}) {
	const items = flattenChildren(children);
	return (
		<div>
			<h3 className="text-foreground mb-2 text-sm font-semibold">{title}</h3>
			{/* Separator under the title — same treatment as Section. */}
			<div className="mb-3">
				<Divider />
			</div>
			<div className="space-y-3">
				{dividers
					? items.map((child, i) => (
							<Fragment key={i}>
								{child}
								{i < items.length - 1 && <Divider />}
							</Fragment>
						))
					: children}
			</div>
		</div>
	);
}

export function TwoCol({
	left,
	right,
	gapX = "default",
}: {
	left: React.ReactNode;
	right: React.ReactNode;
	/** Horizontal gap between the two columns. `wide` (~64px) is used where
	 *  the two cards feel cramped at the default 40px (e.g. Multiple
	 *  Satisfaction). */
	gapX?: "default" | "wide";
}) {
	return (
		<div
			className={cn(
				"grid gap-y-6 md:grid-cols-2",
				gapX === "wide" ? "gap-x-16" : "gap-x-10",
			)}
		>
			<div>{left}</div>
			<div>{right}</div>
		</div>
	);
}

export function KeyVal({
	label,
	value,
}: {
	label: React.ReactNode;
	value: React.ReactNode;
}) {
	return (
		<div className="flex items-start justify-between gap-4">
			<div className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
				{label}
			</div>
			<div className="text-foreground text-right text-sm">{value}</div>
		</div>
	);
}
