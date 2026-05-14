import { cn } from "@/lib/utils";

export function Table({
	className,
	...props
}: React.HTMLAttributes<HTMLTableElement>) {
	return (
		<div className="w-full overflow-x-auto">
			<table
				className={cn("w-full caption-bottom text-sm", className)}
				{...props}
			/>
		</div>
	);
}

export function TableHeader({
	className,
	...props
}: React.HTMLAttributes<HTMLTableSectionElement>) {
	return (
		<thead
			className={cn("border-border border-b [&_tr]:border-b-0", className)}
			{...props}
		/>
	);
}

export function TableBody({
	className,
	...props
}: React.HTMLAttributes<HTMLTableSectionElement>) {
	return (
		<tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />
	);
}

export function TableRow({
	className,
	...props
}: React.HTMLAttributes<HTMLTableRowElement>) {
	return (
		<tr
			className={cn(
				"border-border/60 hover:bg-muted/40 border-b transition-colors",
				className,
			)}
			{...props}
		/>
	);
}

export function TableHead({
	className,
	...props
}: React.ThHTMLAttributes<HTMLTableCellElement>) {
	return (
		<th
			className={cn(
				"text-muted-foreground h-10 px-4 text-left align-middle text-xs font-semibold tracking-wide uppercase",
				className,
			)}
			{...props}
		/>
	);
}

export function TableCell({
	className,
	...props
}: React.TdHTMLAttributes<HTMLTableCellElement>) {
	return (
		<td
			className={cn("text-foreground h-12 px-4 align-middle", className)}
			{...props}
		/>
	);
}
