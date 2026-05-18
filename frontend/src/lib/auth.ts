import { create } from "zustand";
import { persist } from "zustand/middleware";

export type User = {
	fullName: string;
	email: string;
	role?: "Admin" | "Reviewer";
};

type State = {
	user: User | null;
	verified: boolean;
};

type Actions = {
	signUp: (input: { fullName: string; email: string }) => Promise<void>;
	verifyEmail: () => Promise<void>;
	resendEmail: () => Promise<void>;
	logout: () => void;
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const useAuthStore = create<State & Actions>()(
	persist(
		(set) => ({
			user: null,
			verified: false,
			signUp: async (input) => {
				await sleep(600);
				set({ user: { role: "Admin", ...input }, verified: false });
			},
			verifyEmail: async () => {
				await sleep(400);
				set((s) => (s.user ? { verified: true } : s));
			},
			resendEmail: async () => {
				await sleep(500);
			},
			logout: () => set({ user: null, verified: false }),
		}),
		{
			name: "tms-auth",
			partialize: (s) => ({ user: s.user, verified: s.verified }),
		},
	),
);

export function useAuth() {
	const user = useAuthStore((s) => s.user);
	const verified = useAuthStore((s) => s.verified);
	const { signUp, verifyEmail, resendEmail, logout } = useAuthStore.getState();
	return {
		user,
		isAuthenticated: verified,
		signUp,
		verifyEmail,
		resendEmail,
		logout,
	};
}
