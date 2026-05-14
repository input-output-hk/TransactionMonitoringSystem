export const USER_ROLES = ["Admin", "Reviewer"] as const;
export type UserRole = (typeof USER_ROLES)[number];

export type ManagedUser = {
	id: string;
	fullName: string;
	email: string;
	role: UserRole;
};

const NAMES = [
	"Anna Rossi",
	"Bruno Ricci",
	"Carla Conti",
	"Davide Marini",
	"Elena Greco",
	"Fabio Romano",
	"Giulia Russo",
	"Hugo Costa",
	"Irene Lombardi",
	"Jacopo Esposito",
	"Karla Bianchi",
	"Luca Galli",
	"Marta Ferrari",
	"Nina De Luca",
	"Omar Sala",
];

export const seedUsers: ManagedUser[] = NAMES.map((fullName, i) => ({
	id: `user-${String(i + 1).padStart(3, "0")}`,
	fullName,
	email: `${fullName.toLowerCase().replace(/[^a-z]+/g, ".")}@email.com`,
	role: i === 0 ? "Admin" : "Reviewer",
}));
