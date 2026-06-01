import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * `yyyy-mm-dd` date input + label, sized to match other form controls.
 * Used in Reports + Archive filter bars.
 */
export function DateField({
	id,
	label,
	value,
	onChange,
	className,
}: {
	id: string;
	label: string;
	value: string;
	onChange: (v: string) => void;
	className?: string;
}) {
	return (
		<div className={cn("flex flex-col gap-1.5", className)}>
			<Label htmlFor={id} className="text-foreground text-xs">
				{label}
			</Label>
			<input
				id={id}
				type="date"
				value={value}
				onChange={(e) => onChange(e.target.value)}
				className={cn(
					"border-border bg-input/40 text-foreground flex h-11 w-[180px] items-center rounded-sm border px-3 py-2 text-sm transition-colors",
					"focus-visible:ring-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:outline-none",
				)}
			/>
		</div>
	);
}
