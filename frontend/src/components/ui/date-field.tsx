import { Input } from "@/components/ui/input";
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
			<Input
				id={id}
				type="date"
				value={value}
				onChange={(e) => onChange(e.target.value)}
				className="w-[180px]"
			/>
		</div>
	);
}
