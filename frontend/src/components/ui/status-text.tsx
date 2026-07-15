import { cn } from "@/lib/utils";

/**
 * The three inline status lines the tables and panels show while a query is
 * pending, failed, or empty. One component each so the copy stays short and
 * the color/size is uniform: loading and empty are muted, errors are
 * `text-destructive` with `role="alert"` for assistive tech. Larger
 * compositions (banners, cards) style their own text; these are for the plain
 * one-line states.
 */

type Props = {
	children?: React.ReactNode;
	className?: string;
};

export function LoadingText({ children, className }: Props) {
	return (
		<p className={cn("text-muted-foreground text-sm", className)}>
			{children ?? "Loading…"}
		</p>
	);
}

export function ErrorText({ children, className }: Props) {
	return (
		<p role="alert" className={cn("text-destructive text-sm", className)}>
			{children ?? "Failed to load."}
		</p>
	);
}

export function EmptyText({ children, className }: Props) {
	return (
		<p className={cn("text-muted-foreground text-sm", className)}>{children}</p>
	);
}
