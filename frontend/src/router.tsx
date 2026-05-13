import { AppShell } from "@/components/app-shell";
import { RedirectIfAuthed, RequireAuth } from "@/components/route-guards";
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
					{ path: "/reports", element: <ReportsPage /> },
				],
			},
		],
	},
	{ path: "*", element: <Navigate to="/signup" replace /> },
]);
