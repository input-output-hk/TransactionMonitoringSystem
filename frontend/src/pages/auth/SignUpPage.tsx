/**
 * Magic-link login form.
 *
 * The file name retains "SignUp" for git-history continuity, but there
 * is no self-signup any more: only an admin can create accounts (see
 * `UsersPage`). This page lets an existing user request a one-shot
 * magic link to their inbox; verification happens on `/auth/verify`.
 *
 * The submitted email is intentionally NOT used to confirm whether the
 * address exists — the backend always returns 200 to prevent email
 * enumeration. So we always navigate to the "check your email" screen
 * regardless.
 */
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { requestLink } from "@/lib/api/auth";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

export function SignUpPage() {
	const navigate = useNavigate();
	const [email, setEmail] = useState("");
	const [submitting, setSubmitting] = useState(false);
	const [error, setError] = useState<string | null>(null);

	async function onSubmit(e: FormEvent) {
		e.preventDefault();
		if (!email.trim()) return;
		setSubmitting(true);
		setError(null);
		try {
			await requestLink(email.trim());
			// Pass the email forward so the next page can offer "resend" and
			// echo the address back to the user.
			navigate("/verify-email", { state: { email: email.trim() } });
		} catch (err) {
			setError(err instanceof Error ? err.message : "Unable to send link.");
		} finally {
			setSubmitting(false);
		}
	}

	return (
		<div className="bg-background flex min-h-screen items-center justify-center px-4">
			<div className="border-border bg-card w-full max-w-115 rounded-lg border-2 p-10 shadow-sm">
				<h1 className="text-foreground mb-8 text-center text-4xl font-extrabold tracking-tight">
					TMS
				</h1>

				<form onSubmit={onSubmit} className="flex flex-col gap-5">
					<div className="flex flex-col gap-2">
						<Label htmlFor="email">Email</Label>
						<Input
							id="email"
							type="email"
							autoComplete="email"
							value={email}
							onChange={(e) => setEmail(e.target.value)}
							placeholder="user1234@email.com"
							required
						/>
					</div>

					{error && (
						<p className="text-status-offline text-center text-xs">
							{error}
						</p>
					)}

					<div className="mt-6 flex justify-center">
						<Button
							type="submit"
							variant="outline"
							disabled={submitting}
							className="min-w-35"
						>
							{submitting ? "Sending…" : "Send Magic Link"}
						</Button>
					</div>
				</form>
			</div>
		</div>
	);
}
