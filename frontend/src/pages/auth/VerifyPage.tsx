/**
 * Magic-link landing page. Mounted at `/auth/verify`.
 *
 * Flow:
 *   1. Email link points here with `?token=…`.
 *   2. On mount we call `verifyToken(token)` once — the backend
 *      atomically consumes the token, sets the session cookie, and
 *      returns the user.
 *   3. Success → refresh `/me` via the auth context so the rest of the
 *      app picks up the new session, then navigate to `/dashboard`.
 *   4. Failure (invalid / expired / consumed) → show an error message
 *      with a link back to the login form.
 *
 * The `useRef` lock prevents StrictMode from firing the request twice
 * in development; otherwise the second call would race with the first
 * and always hit the "token already consumed" path.
 */
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { verifyToken } from "@/lib/api/auth";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

type Phase = "verifying" | "success" | "error";

export function VerifyPage() {
	const [params] = useSearchParams();
	const token = params.get("token") ?? "";
	const navigate = useNavigate();
	const { refetchUser } = useAuth();

	// Initial state is derived from the token synchronously (no setState in the
	// effect for the missing-token case): a blank token renders the error state
	// on the first paint, which also avoids a cascading re-render.
	const [phase, setPhase] = useState<Phase>(token ? "verifying" : "error");
	const [error, setError] = useState<string | null>(
		token ? null : "This link is missing its token.",
	);
	// Guard against React 18 StrictMode double-mount → token consumption
	// is single-use so the second call would always 400.
	const fired = useRef(false);

	useEffect(() => {
		if (!token) return; // nothing to verify; initial state already shows the error
		if (fired.current) return;
		fired.current = true;

		void (async () => {
			try {
				await verifyToken(token);
				await refetchUser();
				setPhase("success");
				void navigate("/dashboard", { replace: true });
			} catch (err) {
				setPhase("error");
				setError(
					err instanceof Error
						? err.message
						: "This link is invalid or has expired.",
				);
			}
		})();
	}, [token, navigate, refetchUser]);

	return (
		<div className="bg-background flex min-h-screen items-center justify-center px-4">
			<div className="border-border bg-card w-full max-w-115 rounded-lg border-2 p-10 shadow-sm">
				<h1 className="text-foreground mb-8 text-center text-4xl font-extrabold tracking-tight">
					TMS
				</h1>

				{phase === "verifying" && (
					<p className="text-muted-foreground text-center text-sm">
						Verifying your link…
					</p>
				)}

				{phase === "success" && (
					<p className="text-foreground text-center text-sm">
						Signed in — taking you to the dashboard.
					</p>
				)}

				{phase === "error" && (
					<>
						<p className="text-foreground text-center text-sm leading-relaxed">
							{error ?? "This link is invalid or has expired."}
						</p>
						<div className="mt-10 flex justify-center">
							<Link to="/login">
								<Button variant="outline" className="min-w-[160px]">
									Back to sign-in
								</Button>
							</Link>
						</div>
					</>
				)}
			</div>
		</div>
	);
}
