import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";

export const Select = SelectPrimitive.Root;
export const SelectGroup = SelectPrimitive.Group;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({
	className,
	children,
	...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
	return (
		<SelectPrimitive.Trigger
			className={cn(
				"border-border bg-card text-foreground flex h-9 w-full items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-sm",
				"focus:ring-ring focus:ring-offset-background focus:ring-2 focus:ring-offset-1 focus:outline-none",
				"data-[placeholder]:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50",
				"[&>span]:line-clamp-1",
				className,
			)}
			{...props}
		>
			{children}
			<SelectPrimitive.Icon asChild>
				<ChevronDown className="h-4 w-4 opacity-60" />
			</SelectPrimitive.Icon>
		</SelectPrimitive.Trigger>
	);
}

export function SelectContent({
	className,
	children,
	position = "popper",
	...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
	return (
		<SelectPrimitive.Portal>
			<SelectPrimitive.Content
				position={position}
				className={cn(
					"border-border bg-popover text-popover-foreground relative z-50 max-h-(--radix-select-content-available-height) min-w-[8rem] overflow-hidden rounded-lg border shadow-lg",
					position === "popper" &&
						"data-[side=bottom]:translate-y-1 data-[side=top]:-translate-y-1",
					className,
				)}
				{...props}
			>
				<SelectPrimitive.ScrollUpButton className="flex h-6 items-center justify-center">
					<ChevronUp className="h-4 w-4" />
				</SelectPrimitive.ScrollUpButton>
				<SelectPrimitive.Viewport
					className={cn(
						"p-1",
						position === "popper" &&
							"h-(--radix-select-trigger-height) w-full min-w-(--radix-select-trigger-width)",
					)}
				>
					{children}
				</SelectPrimitive.Viewport>
				<SelectPrimitive.ScrollDownButton className="flex h-6 items-center justify-center">
					<ChevronDown className="h-4 w-4" />
				</SelectPrimitive.ScrollDownButton>
			</SelectPrimitive.Content>
		</SelectPrimitive.Portal>
	);
}

export function SelectItem({
	className,
	children,
	...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
	return (
		<SelectPrimitive.Item
			className={cn(
				"focus:bg-accent focus:text-accent-foreground relative flex w-full cursor-pointer items-center rounded-md py-1.5 pr-2 pl-8 text-sm outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
				className,
			)}
			{...props}
		>
			<span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
				<SelectPrimitive.ItemIndicator>
					<Check className="h-4 w-4" />
				</SelectPrimitive.ItemIndicator>
			</span>
			<SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
		</SelectPrimitive.Item>
	);
}

export function SelectSeparator({
	className,
	...props
}: React.ComponentProps<typeof SelectPrimitive.Separator>) {
	return (
		<SelectPrimitive.Separator
			className={cn("bg-border -mx-1 my-1 h-px", className)}
			{...props}
		/>
	);
}
