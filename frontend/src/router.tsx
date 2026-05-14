import { AppShell } from "@/components/app-shell";
import { RedirectIfAuthed, RequireAuth } from "@/components/route-guards";
import { ArchivePage } from "@/pages/ArchivePage";
import { AttackDetailPage } from "@/pages/AttackDetailPage";
import { AttacksPage } from "@/pages/AttacksPage";
import { ReportsPage } from "@/pages/ReportsPage";
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
					{ path: "/attacks/:id", element: <AttackDetailPage /> },
					{ path: "/reports", element: <ReportsPage /> },
					{ path: "/archive", element: <ArchivePage /> },
					{
						path: "/archive/:id",
						element: <AttackDetailPage archived />,
					},
				],
			},
		],
	},
	{ path: "*", element: <Navigate to="/signup" replace /> },
]);
