/**
 * Initials from a full name. Picks the first letter of up to `max` words.
 * Empty / missing names fall back to `"U"` so avatars are never blank.
 */
export function initials(
	name: string | undefined | null,
	max: number = 2,
): string {
	if (!name) return "U";
	const parts = name
		.split(/\s+/)
		.filter(Boolean)
		.slice(0, max)
		.map((p) => p[0]?.toUpperCase() ?? "");
	return parts.join("") || "U";
}

/** Truncate a tx hash like Figma's `xxxxxxxxxxxx...xxxxxxxx` style. */
export function shortHash(hash: string): string {
	if (hash.length <= 20) return hash;
	return `${hash.slice(0, 12)}...${hash.slice(-8)}`;
}
