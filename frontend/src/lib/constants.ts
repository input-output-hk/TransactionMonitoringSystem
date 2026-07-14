// Shared UI constants.

// Default rows-per-page for paginated tables. Single-sourced here because the
// same value was repeated as a bare literal across the Attacks / Archive /
// Reports / Users pages.
export const DEFAULT_PAGE_SIZE = 10;

// The page-size picker's choices (TableFooter's default set). Shared so a
// page that persists the size in the URL validates against the same list
// the picker offers.
export const PAGE_SIZE_OPTIONS: readonly number[] = [10, 25, 50];
