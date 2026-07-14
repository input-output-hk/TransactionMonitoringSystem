/**
 * "Check your inbox" confirmation shown after the user requests a
 * magic-link login. The actual verification happens on `/auth/verify`
 * when the user clicks the link from their email — this page just
 * confirms that the request was accepted and offers a resend.
 *
 * The email address is passed via `location.state.email` from the
 * login form. If a user lands here directly (e.g. via reload) we
 * fall back to a generic message rather than echoing nothing.
 */
import { Button } from "@/components/ui/button";
import { requestLink } from "@/lib/api/auth";
import { useState } from "react";
import { useLocation } from "react-router-dom";

export function EmailSentPage() {
	const location = useLocation();
	const emailFromState = (location.state as { email?: string } | null)?.email;
	const [resending, setResending] = useState(false);
	const [resent, setResent] = useState(false);
	const [error, setError] = useState<string | null>(null);

	async function onResend() {
		if (!emailFromState) {
			setError(
				"Refresh and submit your email again — we lost the address from your previous step.",
			);
			return;
		}
		setResending(true);
		setError(null);
		try {
			await requestLink(emailFromState);
			setResent(true);
		} catch (err) {
			setError(err instanceof Error ? err.message : "Resend failed.");
		} finally {
			setResending(false);
		}
	}

	return (
		<div className="bg-background flex min-h-screen items-center justify-center px-4">
			<div className="border-border bg-card w-full max-w-115 rounded-lg border-2 p-10 shadow-sm">
				<h1 className="text-foreground mb-12 text-center text-4xl font-extrabold tracking-tight">
					TMS
				</h1>

				<p className="text-foreground text-center text-sm leading-relaxed">
					An email has been successfully sent
					{emailFromState ? ` to ${emailFromState}` : ""}.
					<br />
					Click the link inside to sign in. If you don't see it, check
					your spam folder or click resend.
				</p>

				{error && (
					<p className="text-status-offline mt-4 text-center text-xs">
						{error}
					</p>
				)}

				<div className="mt-16 flex justify-center">
					<Button
						type="button"
						variant="outline"
						onClick={() => void onResend()}
						disabled={resending || !emailFromState}
						className="min-w-[140px]"
					>
						{resending ? "Sending…" : resent ? "Sent ✓" : "Resend"}
					</Button>
				</div>
			</div>
		</div>
	);
}
