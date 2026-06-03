import { AppShell } from "@/components/app-shell";
import {
	RedirectIfAuthed,
	RequireAdmin,
	RequireAuth,
} from "@/components/route-guards";
import { ArchivePage } from "@/pages/ArchivePage";
import { AttacksPage } from "@/pages/AttacksPage";
import { ImportAttackPage } from "@/pages/ImportAttackPage";
import { ReportsPage } from "@/pages/ReportsPage";
import { UsersPage } from "@/pages/UsersPage";
import { EmailSentPage } from "@/pages/auth/EmailSentPage";
import { SignUpPage as LoginPage } from "@/pages/auth/SignUpPage";
import { VerifyPage } from "@/pages/auth/VerifyPage";
import { Navigate, createBrowserRouter } from "react-router-dom";

export const router = createBrowserRouter([
	// `/auth/verify` is BOTH public and "ignore current auth state" — a user
	// might be silently logged-in from a previous session and still want to
	// redeem a new magic link (e.g. role bump). Placed outside the auth
	// guards so the redirect-if-authed wrapper doesn't kick them away from
	// their own verification flow.
	{ path: "/auth/verify", element: <VerifyPage /> },
	{
		element: <RedirectIfAuthed />,
		children: [
			{ path: "/", element: <Navigate to="/login" replace /> },
			{ path: "/login", element: <LoginPage /> },
			// `/signup` kept as an alias of `/login` so old links / bookmarks
			// from the mock-auth era keep working. Pure backwards-compat —
			// remove once no live env has the old route cached.
			{ path: "/signup", element: <Navigate to="/login" replace /> },
			{ path: "/verify-email", element: <EmailSentPage /> },
		],
	},
	{
		element: <RequireAuth />,
		children: [
			{
				element: <AppShell />,
				children: [
					{ path: "/dashboard", element: <AttacksPage /> },
					// Same component as /dashboard — AttacksPage reads `:id` from
					// useParams and renders the AttackDetailPage inside a Dialog
					// overlay, so the dashboard stays mounted (and dimmed) under
					// the popup.
					{ path: "/attacks/:id", element: <AttacksPage /> },
					{ path: "/reports", element: <ReportsPage /> },
					{ path: "/archive", element: <ArchivePage /> },
					// Same component as /archive — ArchivePage reads `:id` from
					// useParams and renders the AttackDetailPage (archived view)
					// in a Dialog overlay so the table behind stays mounted.
					{ path: "/archive/:id", element: <ArchivePage /> },
					// /users is wrapped in a stricter Admin guard. Reviewers
					// land here from a typed URL → silent redirect to dashboard.
					{
						element: <RequireAdmin />,
						children: [{ path: "/users", element: <UsersPage /> }],
					},
					{ path: "/import", element: <ImportAttackPage /> },
				],
			},
		],
	},
	{ path: "*", element: <Navigate to="/login" replace /> },
]);
