import { cn } from "@/lib/utils";

export function Label({
	className,
	...props
}: React.LabelHTMLAttributes<HTMLLabelElement>) {
	return (
		<label
			className={cn(
				"text-foreground text-sm leading-none font-semibold",
				className,
			)}
			{...props}
		/>
	);
}
