import { cn } from "@/lib/utils";

/**
 * Small icon button used by table footer paginators (first / prev / next /
 * last). Same visual everywhere — Reports, Users, Archive.
 *
 * Pass `aria-label` via props for accessibility.
 */
export function PageBtn({
	children,
	onClick,
	disabled,
	className,
	...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
	return (
		<button
			type="button"
			onClick={onClick}
			disabled={disabled}
			className={cn(
				"inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors",
				"hover:bg-accent hover:text-foreground",
				"disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
				className,
			)}
			{...props}
		>
			{children}
		</button>
	);
}
