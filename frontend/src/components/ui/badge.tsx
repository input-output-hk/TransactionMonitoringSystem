import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
	"inline-flex items-center justify-center rounded-[4px] border-2 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wider",
	{
		variants: {
			variant: {
				default: "border-transparent bg-secondary text-secondary-foreground",
				outline: "border-border text-foreground",
				low: "border-severity-low-foreground/60 bg-severity-low/25 text-severity-low-foreground",
				medium:
					"border-severity-medium-foreground/60 bg-severity-medium/25 text-severity-medium-foreground",
				high: "border-severity-high-foreground/60 bg-severity-high/25 text-severity-high-foreground",
				critical:
					"border-severity-critical-foreground/60 bg-severity-critical/25 text-severity-critical-foreground",
			},
		},
		defaultVariants: { variant: "default" },
	},
);

export interface BadgeProps
	extends
		React.HTMLAttributes<HTMLSpanElement>,
		VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
	return (
		<span className={cn(badgeVariants({ variant }), className)} {...props} />
	);
}
