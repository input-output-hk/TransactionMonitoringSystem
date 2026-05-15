import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

export function EmailSentPage() {
	const navigate = useNavigate();
	const { resendEmail, verifyEmail, user } = useAuth();
	const [resending, setResending] = useState(false);
	const [resent, setResent] = useState(false);

	async function onResend() {
		setResending(true);
		try {
			await resendEmail();
			setResent(true);
		} finally {
			setResending(false);
		}
	}

	// Mock: clicking the title 3x simulates verification (since there's no real email)
	async function onMockVerify() {
		await verifyEmail();
		navigate("/dashboard");
	}

	return (
		<div className="bg-background flex min-h-screen items-center justify-center px-4">
			<div className="border-border bg-card w-full max-w-115 rounded-2xl border p-10 shadow-sm">
				<h1
					onClick={onMockVerify}
					title="(mock) click to verify"
					className="text-foreground mb-12 cursor-pointer text-center text-4xl font-extrabold tracking-tight select-none"
				>
					TMS
				</h1>

				<p className="text-foreground text-center text-sm leading-relaxed">
					An email has been successfully sent
					{user?.email ? ` to ${user.email}` : ""}.
					<br />
					If you did not receive please click resend.
				</p>

				<div className="mt-16 flex justify-center">
					<Button
						type="button"
						variant="outline"
						onClick={onResend}
						disabled={resending}
						className="min-w-[140px]"
					>
						{resending ? "Sending…" : resent ? "Sent ✓" : "Resend"}
					</Button>
				</div>
			</div>
		</div>
	);
}
