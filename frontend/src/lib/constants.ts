// Shared UI constants.

// Default rows-per-page for paginated tables. Single-sourced here because the
// same value was repeated as a bare literal across the Attacks / Archive /
// Reports / Users pages.
export const DEFAULT_PAGE_SIZE = 10;

// The page-size picker's choices (TableFooter's default set). Shared so a
// page that persists the size in the URL validates against the same list
// the picker offers.
export const PAGE_SIZE_OPTIONS: readonly number[] = [10, 25, 50];

// The lowest max_score the alert list requests. A scored transaction sits at
// exactly 0 when every detector either gated out or found nothing, which is the
// bulk of ingested traffic; those rows are queryable but are not alerts. Mirrors
// the backend's FINDING_MIN_SCORE (app/models/transaction.py), which the Avg Risk
// aggregate applies too so the KPI and this table describe one population.
export const FINDING_MIN_SCORE = 1;
