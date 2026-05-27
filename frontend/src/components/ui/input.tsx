import { cn } from "@/lib/utils";

export function Input({
	className,
	type = "text",
	...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
	return (
		<input
			type={type}
			className={cn(
				// Fill matches the surrounding card (`bg-card`) — keeps the input
				// visually flush with the panel it's sitting in, while the
				// border (border-input/border-border at oklch 0.50) is the only
				// thing that delineates it.
				"border-border bg-card text-foreground placeholder:text-muted-foreground flex h-11 w-full rounded-sm border px-3 py-2 text-sm transition-colors",
				"focus-visible:ring-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:outline-none",
				"disabled:cursor-not-allowed disabled:opacity-50",
				className,
			)}
			{...props}
		/>
	);
}
