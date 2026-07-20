/**
 * Shared affordance for clustering mutation controls rendered to a Reviewer
 * session. The backend proxy rejects clustering POST/PATCH/DELETE from
 * non-Admin sessions with a 403 (see backend `app/api/clustering.py`), so the
 * UI disables those controls up front and explains why.
 */
import type { ReactNode } from "react";

export const ADMIN_ONLY_HINT =
	"Admin only: your account has read-only access to clustering.";

/**
 * Wrap a disabled, Admin-only control so its explanation is actually visible.
 * A bare `title` on a disabled `<button>` is suppressed by browsers (a disabled
 * control receives no pointer events, so the tooltip never fires), which would
 * leave a Reviewer staring at a greyed-out button with no reason. Hanging the
 * `title` on a wrapping `<span>` and neutralizing the child's pointer events
 * makes the hover resolve to the span, so the tooltip shows.
 *
 * When `gated` is false this is a transparent passthrough, so call sites can
 * wrap unconditionally and let the flag decide.
 */
export function AdminOnlyGate({
	gated,
	children,
}: {
	gated: boolean;
	children: ReactNode;
}) {
	if (!gated) return <>{children}</>;
	return (
		<span
			title={ADMIN_ONLY_HINT}
			className="inline-flex cursor-not-allowed [&>*]:pointer-events-none"
		>
			{children}
		</span>
	);
}
