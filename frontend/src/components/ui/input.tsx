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
				"border-border bg-input/40 text-foreground placeholder:text-muted-foreground flex h-11 w-full rounded-md border px-3 py-2 text-sm transition-colors",
				"focus-visible:ring-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:outline-none",
				"disabled:cursor-not-allowed disabled:opacity-50",
				className,
			)}
			{...props}
		/>
	);
}
