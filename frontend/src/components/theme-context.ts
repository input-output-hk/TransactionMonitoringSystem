import { createContext, useContext } from "react";

export type Theme = "light" | "dark";

export type ThemeContextValue = {
	theme: Theme;
	setTheme: (t: Theme) => void;
	toggleTheme: () => void;
	/** True when the user has explicitly picked a theme; false when following system. */
	isUserOverride: boolean;
	/** Discard the user choice and follow the OS again. */
	resetToSystem: () => void;
};

export const ThemeContext = createContext<ThemeContextValue | null>(null);

export function useTheme() {
	const ctx = useContext(ThemeContext);
	if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
	return ctx;
}
