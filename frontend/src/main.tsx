import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import {
	MutationCache,
	QueryCache,
	QueryClient,
	QueryClientProvider,
} from "@tanstack/react-query";
import "./index.css";
import { AuthProvider } from "@/components/auth-provider";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ME_QUERY_KEY } from "@/lib/api/auth";
import { UnauthorizedError } from "@/lib/api/fetch";
import { router } from "@/router";

/**
 * Global 401 handling: when any query or mutation against a protected
 * endpoint fails with UnauthorizedError (session expired or revoked
 * server-side), reset the cached `/me` user to anonymous. RequireAuth
 * observes that and redirects to /login. The `/me` query itself never
 * throws this (it maps 401 to null), so there is no feedback loop.
 */
function onUnauthorized(error: unknown) {
	if (error instanceof UnauthorizedError) {
		queryClient.setQueryData(ME_QUERY_KEY, null);
	}
}

const queryClient = new QueryClient({
	queryCache: new QueryCache({ onError: onUnauthorized }),
	mutationCache: new MutationCache({ onError: onUnauthorized }),
	defaultOptions: {
		queries: { staleTime: 30_000, refetchOnWindowFocus: false },
	},
});

createRoot(document.getElementById("root")!).render(
	<StrictMode>
		<ThemeProvider>
			<QueryClientProvider client={queryClient}>
				<AuthProvider>
					<TooltipProvider delayDuration={200}>
						<RouterProvider router={router} />
						<Toaster />
					</TooltipProvider>
				</AuthProvider>
			</QueryClientProvider>
		</ThemeProvider>
	</StrictMode>,
);
