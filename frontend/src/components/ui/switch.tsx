import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/utils";

/**
 * On/off switch (Radix). Deliberately a different control type from the
 * checkbox: a Switch powers a feature on or off (e.g. a notification
 * channel), while checkboxes select routing within an already-powered
 * feature.
 */
export function Switch({
	className,
	...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
	return (
		<SwitchPrimitive.Root
			className={cn(
				"data-[state=checked]:bg-brand data-[state=unchecked]:bg-input focus-visible:ring-ring inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50",
				className,
			)}
			{...props}
		>
			{/* Thumb travel is derived from the geometry above: 36px track (w-9)
			    − 16px thumb (h-4 w-4) − 2px inset per side ⇒ rest at 2px, checked
			    at 18px. Resize track/thumb and these travel stops in lockstep. */}
			<SwitchPrimitive.Thumb className="bg-card pointer-events-none block h-4 w-4 rounded-full shadow-sm transition-transform data-[state=checked]:translate-x-[18px] data-[state=unchecked]:translate-x-[2px]" />
		</SwitchPrimitive.Root>
	);
}
