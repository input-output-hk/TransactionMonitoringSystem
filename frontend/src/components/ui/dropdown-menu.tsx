import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import { Check, ChevronRight, Circle } from "lucide-react";
import { cn } from "@/lib/utils";

export const DropdownMenu = DropdownMenuPrimitive.Root;
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;
export const DropdownMenuGroup = DropdownMenuPrimitive.Group;
export const DropdownMenuPortal = DropdownMenuPrimitive.Portal;
export const DropdownMenuSub = DropdownMenuPrimitive.Sub;
export const DropdownMenuRadioGroup = DropdownMenuPrimitive.RadioGroup;

export function DropdownMenuContent({
	className,
	sideOffset = 6,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content>) {
	return (
		<DropdownMenuPrimitive.Portal>
			<DropdownMenuPrimitive.Content
				sideOffset={sideOffset}
				className={cn(
					"border-border bg-popover text-popover-foreground z-50 min-w-[12rem] overflow-hidden rounded-lg border p-1 shadow-lg",
					"data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
					className,
				)}
				{...props}
			/>
		</DropdownMenuPrimitive.Portal>
	);
}

export function DropdownMenuItem({
	className,
	inset,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item> & {
	inset?: boolean;
}) {
	return (
		<DropdownMenuPrimitive.Item
			className={cn(
				"relative flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 text-sm outline-none select-none",
				"focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
				inset && "pl-8",
				className,
			)}
			{...props}
		/>
	);
}

export function DropdownMenuLabel({
	className,
	inset,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Label> & {
	inset?: boolean;
}) {
	return (
		<DropdownMenuPrimitive.Label
			className={cn(
				"text-muted-foreground px-2.5 py-1.5 text-xs font-semibold tracking-wide uppercase",
				inset && "pl-8",
				className,
			)}
			{...props}
		/>
	);
}

export function DropdownMenuSeparator({
	className,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Separator>) {
	return (
		<DropdownMenuPrimitive.Separator
			className={cn("bg-border -mx-1 my-1 h-px", className)}
			{...props}
		/>
	);
}

export function DropdownMenuCheckboxItem({
	className,
	children,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.CheckboxItem>) {
	return (
		<DropdownMenuPrimitive.CheckboxItem
			className={cn(
				"focus:bg-accent focus:text-accent-foreground relative flex cursor-pointer items-center rounded-md py-1.5 pr-2 pl-8 text-sm outline-none select-none",
				className,
			)}
			{...props}
		>
			<span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
				<DropdownMenuPrimitive.ItemIndicator>
					<Check className="h-3.5 w-3.5" />
				</DropdownMenuPrimitive.ItemIndicator>
			</span>
			{children}
		</DropdownMenuPrimitive.CheckboxItem>
	);
}

export function DropdownMenuRadioItem({
	className,
	children,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.RadioItem>) {
	return (
		<DropdownMenuPrimitive.RadioItem
			className={cn(
				"focus:bg-accent focus:text-accent-foreground relative flex cursor-pointer items-center rounded-md py-1.5 pr-2 pl-8 text-sm outline-none select-none",
				className,
			)}
			{...props}
		>
			<span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
				<DropdownMenuPrimitive.ItemIndicator>
					<Circle className="h-2 w-2 fill-current" />
				</DropdownMenuPrimitive.ItemIndicator>
			</span>
			{children}
		</DropdownMenuPrimitive.RadioItem>
	);
}

export function DropdownMenuSubTrigger({
	className,
	inset,
	children,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubTrigger> & {
	inset?: boolean;
}) {
	return (
		<DropdownMenuPrimitive.SubTrigger
			className={cn(
				"focus:bg-accent data-[state=open]:bg-accent flex cursor-pointer items-center rounded-md px-2.5 py-1.5 text-sm outline-none select-none",
				inset && "pl-8",
				className,
			)}
			{...props}
		>
			{children}
			<ChevronRight className="ml-auto h-4 w-4" />
		</DropdownMenuPrimitive.SubTrigger>
	);
}

export function DropdownMenuSubContent({
	className,
	...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubContent>) {
	return (
		<DropdownMenuPrimitive.SubContent
			className={cn(
				"border-border bg-popover text-popover-foreground z-50 min-w-[8rem] overflow-hidden rounded-lg border p-1 shadow-lg",
				className,
			)}
			{...props}
		/>
	);
}
