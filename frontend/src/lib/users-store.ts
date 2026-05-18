import { create } from "zustand";
import { persist } from "zustand/middleware";
import { seedUsers, type ManagedUser, type UserRole } from "@/mocks/users";

type State = {
	users: ManagedUser[];
};

type Actions = {
	addUser: (input: {
		fullName: string;
		email: string;
		role: UserRole;
	}) => ManagedUser;
	removeUser: (id: string) => void;
};

const useUsersStoreRaw = create<State & Actions>()(
	persist(
		(set, get) => ({
			users: seedUsers,
			addUser: (input) => {
				const user: ManagedUser = { id: `user-${Date.now()}`, ...input };
				set({ users: [user, ...get().users] });
				return user;
			},
			removeUser: (id) => {
				set({ users: get().users.filter((u) => u.id !== id) });
			},
		}),
		{
			name: "tms-users",
			partialize: (s) => ({ users: s.users }),
		},
	),
);

export function useUsers(): ManagedUser[] {
	return useUsersStoreRaw((s) => s.users);
}

export const addUser = (input: {
	fullName: string;
	email: string;
	role: UserRole;
}) => useUsersStoreRaw.getState().addUser(input);

export const removeUser = (id: string) =>
	useUsersStoreRaw.getState().removeUser(id);

export function getUser(id: string): ManagedUser | undefined {
	return useUsersStoreRaw.getState().users.find((u) => u.id === id);
}
