/**
 * Multi-select dropdown built on top of `DropdownMenu` + `DropdownMenuCheckboxItem`.
 *
 * Trigger button is styled to match `SelectTrigger` (same height, border,
 * focus ring) so the two can coexist visually in a filter bar without one
 * looking out of place against the other.
 *
 * Selection model: array of string values. Empty array means "no filter";
 * the page is responsible for interpreting that.
 */
import {
	DropdownMenu,
	DropdownMenuCheckboxItem,
	DropdownMenuContent,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { Check, ChevronDown } from "lucide-react";

export type MultiSelectOption<T extends string = string> = {
	value: T;
	label: string;
};

export type MultiSelectProps<T extends string = string> = {
	options: MultiSelectOption<T>[];
	/** Currently selected values. */
	value: T[];
	/** Called with the new selection on every change. */
	onChange: (next: T[]) => void;
	/** Shown on the trigger when nothing is selected. */
	placeholder?: string;
	/**
	 * Singular noun for the trigger summary when 2+ are selected.
	 * `"severity"` → `"3 severities"`. If `pluralLabel` is also passed,
	 * we use that verbatim instead of pluralizing.
	 */
	label?: string;
	pluralLabel?: string;
	/** Tailwind width class. Defaults to `w-[160px]` to match SelectTrigger. */
	className?: string;
	disabled?: boolean;
};

/**
 * Render summary text for the trigger:
 *  - 0 selected: `placeholder` (or `"All"`)
 *  - 1 selected: that option's label
 *  - 2+ selected: `"N <plural>"`
 */
function summarize<T extends string>(
	value: T[],
	options: MultiSelectOption<T>[],
	placeholder: string,
	label: string,
	pluralLabel: string | undefined,
): string {
	if (value.length === 0) return placeholder;
	if (value.length === 1) {
		const opt = options.find((o) => o.value === value[0]);
		return opt?.label ?? value[0];
	}
	const plural = pluralLabel ?? `${label}s`;
	return `${value.length} ${plural}`;
}

export function MultiSelect<T extends string = string>({
	options,
	value,
	onChange,
	placeholder = "All",
	label = "item",
	pluralLabel,
	className,
	disabled,
}: MultiSelectProps<T>) {
	const toggle = (v: T) => {
		const set = new Set(value);
		if (set.has(v)) set.delete(v);
		else set.add(v);
		// Preserve the original option order so the array shape is stable
		// across toggles — easier for query-key equality in React Query.
		const known = options.map((o) => o.value).filter((x) => set.has(x));
		// Keep any currently-selected values that aren't in `options` (e.g. a
		// stored config value the caller's option list doesn't include) so a
		// toggle never silently drops them — preserves the verbatim round-trip.
		const extra = [...set].filter((x) => !options.some((o) => o.value === x));
		onChange([...known, ...extra]);
	};

	const summary = summarize(value, options, placeholder, label, pluralLabel);

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild disabled={disabled}>
				<button
					type="button"
					className={cn(
						// Match SelectTrigger styling so the two live happily together.
						"border-input ring-offset-background placeholder:text-muted-foreground focus-visible:ring-ring inline-flex h-8 w-40 items-center justify-between gap-2 rounded-md border bg-transparent px-3 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50",
						className,
					)}
					aria-label={placeholder}
				>
					<span
						className={cn(
							"truncate text-left",
							value.length === 0 && "text-muted-foreground",
						)}
					>
						{summary}
					</span>
					<ChevronDown className="text-muted-foreground h-4 w-4 shrink-0" />
				</button>
			</DropdownMenuTrigger>
			<DropdownMenuContent
				align="start"
				// Match the trigger width so the popup feels anchored to it.
				className="min-w-(--radix-dropdown-menu-trigger-width)"
			>
				{value.length > 0 && (
					<button
						type="button"
						onClick={() => onChange([])}
						className="text-muted-foreground hover:bg-accent hover:text-foreground flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-xs"
					>
						Clear
						<Check className="h-3 w-3 opacity-0" />
					</button>
				)}
				{options.map((opt) => (
					<DropdownMenuCheckboxItem
						key={opt.value}
						checked={value.includes(opt.value)}
						// Don't auto-close on each pick — users typically want to
						// tick multiple in a row.
						onSelect={(e) => e.preventDefault()}
						onCheckedChange={() => toggle(opt.value)}
					>
						{opt.label}
					</DropdownMenuCheckboxItem>
				))}
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
