import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export function TabsList({
	className,
	...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
	return (
		<TabsPrimitive.List
			className={cn(
				"border-border text-muted-foreground inline-flex h-10 items-center gap-1 border-b",
				className,
			)}
			{...props}
		/>
	);
}

export function TabsTrigger({
	className,
	...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
	return (
		<TabsPrimitive.Trigger
			className={cn(
				// An underline-style tab: the active trigger gets a brand underline and
				// foreground text; inactive triggers stay muted.
				"focus-visible:ring-ring relative -mb-px inline-flex h-10 items-center border-b-2 border-transparent px-3 text-sm font-medium whitespace-nowrap transition-colors outline-none",
				"hover:text-foreground focus-visible:ring-2 disabled:pointer-events-none disabled:opacity-50",
				"data-[state=active]:border-brand data-[state=active]:text-foreground",
				className,
			)}
			{...props}
		/>
	);
}

export function TabsContent({
	className,
	...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
	return (
		<TabsPrimitive.Content
			className={cn(
				"focus-visible:ring-ring mt-4 outline-none focus-visible:ring-2",
				className,
			)}
			{...props}
		/>
	);
}
