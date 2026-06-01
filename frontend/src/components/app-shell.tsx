import { Outlet } from "react-router-dom";
import { TopNav } from "./top-nav";

export function AppShell() {
	return (
		<div className="bg-background text-foreground min-h-screen">
			<TopNav />
			<main className="mx-auto w-full max-w-[1400px] px-6 py-6">
				<Outlet />
			</main>
		</div>
	);
}
