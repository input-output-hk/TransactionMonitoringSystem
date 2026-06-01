/**
 * Shared bottom bar for paginated tables across the app (Dashboard,
 * Archive, Reports). Layout matches the Figma "Risk Allerts" pager:
 *
 *   Show Rows ▾ 10        Center label: N        ◁  <  Page 1 of 500  >  ▷
 *
 * Visuals fixed in one place so the three pages stay aligned without
 * the per-page footer JSX drifting.
 */
import { PageBtn } from "@/components/ui/page-button";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { ChevronLeft, ChevronRight } from "lucide-react";

/** Hollow triangle pointing LEFT — first-page slot. */
function TriangleLeftIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 16 16"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<polygon
				points="10,3 4,8 10,13"
				stroke="currentColor"
				strokeWidth="1.5"
				strokeLinejoin="round"
				strokeLinecap="round"
				fill="none"
			/>
		</svg>
	);
}

/** Mirror — last-page slot. */
function TriangleRightIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 16 16"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<polygon
				points="6,3 12,8 6,13"
				stroke="currentColor"
				strokeWidth="1.5"
				strokeLinejoin="round"
				strokeLinecap="round"
				fill="none"
			/>
		</svg>
	);
}

export type TableFooterProps = {
	pageSize: number;
	onPageSizeChange: (size: number) => void;
	/** Picker values. Default `[10, 25, 50]`. */
	pageSizeOptions?: number[];
	/** Middle content, e.g. `"Total Risk Alerts Shown: 10"`. */
	centerLabel: React.ReactNode;
	/** Zero-based current page index. */
	page: number;
	pageCount: number;
	onPageChange: (page: number) => void;
};

export function TableFooter({
	pageSize,
	onPageSizeChange,
	pageSizeOptions = [10, 25, 50],
	centerLabel,
	page,
	pageCount,
	onPageChange,
}: TableFooterProps) {
	const clampedPage = Math.min(Math.max(page, 0), Math.max(pageCount - 1, 0));
	const isFirst = clampedPage === 0;
	const isLast = clampedPage >= pageCount - 1;

	return (
		<footer className="border-border text-muted-foreground flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-xs">
			{/* Show Rows — inline pseudo-dropdown, no box, just text + chevron */}
			<div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
				<span>Show Rows</span>
				<Select
					value={String(pageSize)}
					onValueChange={(v) => onPageSizeChange(Number(v))}
				>
					<SelectTrigger
						// Strip the boxed default so the trigger looks like
						// "10 ▾" inline next to the label, matching Figma.
						className="text-muted-foreground hover:text-foreground h-auto border-none bg-transparent p-0 text-xs shadow-none focus:ring-0 focus-visible:ring-0"
					>
						<SelectValue />
					</SelectTrigger>
					<SelectContent>
						{pageSizeOptions.map((n) => (
							<SelectItem key={n} value={String(n)}>
								{n}
							</SelectItem>
						))}
					</SelectContent>
				</Select>
			</div>

			<div>{centerLabel}</div>

			<div className="flex items-center gap-1">
				<PageBtn
					aria-label="First page"
					disabled={isFirst}
					onClick={() => onPageChange(0)}
				>
					<TriangleLeftIcon className="h-3.5 w-3.5" />
				</PageBtn>
				<PageBtn
					aria-label="Previous page"
					disabled={isFirst}
					onClick={() => onPageChange(Math.max(0, clampedPage - 1))}
				>
					<ChevronLeft className="h-3.5 w-3.5" />
				</PageBtn>
				<span className="px-2">
					Page {clampedPage + 1} of {Math.max(pageCount, 1)}
				</span>
				<PageBtn
					aria-label="Next page"
					disabled={isLast}
					onClick={() =>
						onPageChange(Math.min(pageCount - 1, clampedPage + 1))
					}
				>
					<ChevronRight className="h-3.5 w-3.5" />
				</PageBtn>
				<PageBtn
					aria-label="Last page"
					disabled={isLast}
					onClick={() => onPageChange(Math.max(pageCount - 1, 0))}
				>
					<TriangleRightIcon className="h-3.5 w-3.5" />
				</PageBtn>
			</div>
		</footer>
	);
}
