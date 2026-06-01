import { AppShell } from "@/components/app-shell";
import { RedirectIfAuthed, RequireAuth } from "@/components/route-guards";
import { ArchivePage } from "@/pages/ArchivePage";
import { AttacksPage } from "@/pages/AttacksPage";
import { ImportAttackPage } from "@/pages/ImportAttackPage";
import { ReportsPage } from "@/pages/ReportsPage";
import { UsersPage } from "@/pages/UsersPage";
import { EmailSentPage } from "@/pages/auth/EmailSentPage";
import { SignUpPage } from "@/pages/auth/SignUpPage";
import { Navigate, createBrowserRouter } from "react-router-dom";

export const router = createBrowserRouter([
	{
		element: <RedirectIfAuthed />,
		children: [
			{ path: "/", element: <Navigate to="/signup" replace /> },
			{ path: "/signup", element: <SignUpPage /> },
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
					{ path: "/users", element: <UsersPage /> },
					{ path: "/import", element: <ImportAttackPage /> },
				],
			},
		],
	},
	{ path: "*", element: <Navigate to="/signup" replace /> },
]);
