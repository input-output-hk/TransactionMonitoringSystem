import { cn } from "@/lib/utils";

export function Card({
	className,
	...props
}: React.HTMLAttributes<HTMLDivElement>) {
	return (
		<div
			className={cn(
				"border-border bg-card text-card-foreground rounded-xl border shadow-sm",
				className,
			)}
			{...props}
		/>
	);
}

export function CardHeader({
	className,
	...props
}: React.HTMLAttributes<HTMLDivElement>) {
	return (
		<div className={cn("flex flex-col gap-1.5 p-6", className)} {...props} />
	);
}

export function CardTitle({
	className,
	...props
}: React.HTMLAttributes<HTMLDivElement>) {
	return (
		<div
			className={cn(
				"text-2xl leading-none font-bold tracking-tight",
				className,
			)}
			{...props}
		/>
	);
}

export function CardContent({
	className,
	...props
}: React.HTMLAttributes<HTMLDivElement>) {
	return <div className={cn("p-6 pt-0", className)} {...props} />;
}

export function CardFooter({
	className,
	...props
}: React.HTMLAttributes<HTMLDivElement>) {
	return (
		<div className={cn("flex items-center p-6 pt-0", className)} {...props} />
	);
}
