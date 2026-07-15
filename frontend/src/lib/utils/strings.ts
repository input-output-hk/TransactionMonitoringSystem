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

/**
 * Middle-truncate a hash/address to `head` + `...` + `tail` chars. The single
 * project-wide truncation helper: callers pass head/tail to match their column
 * width instead of hand-rolling their own slicing with a different glyph.
 */
export function shortHash(s: string, head = 12, tail = 8): string {
	// 3 = the "..." glyph; below this a truncated string would be no shorter.
	if (s.length <= head + tail + 3) return s;
	return `${s.slice(0, head)}...${s.slice(-tail)}`;
}
