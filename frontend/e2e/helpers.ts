import { createHash } from "node:crypto";
import { text } from "node:stream/consumers";
import type { Download } from "@playwright/test";

/** The body of a browser download, as text (CSV exports). */
export async function downloadText(download: Download): Promise<string> {
	return text(await download.createReadStream());
}

/**
 * The seeded findings (backend/scripts/e2e/seed.py): index -> deterministic
 * transaction hash, so specs can address exact rows without an API lookup.
 */
export function seededTxHash(i: number): string {
	return createHash("sha256").update(`tms-e2e-${i}`).digest("hex");
}

/** A deterministic 64-hex hash for CSV-import fixtures (never seeded). */
export function importTxHash(i: number): string {
	return createHash("sha256").update(`tms-e2e-import-${i}`).digest("hex");
}

/**
 * The dashboard renders hashes as first 12 + "..." + last 8 characters
 * (frontend/src/utils/strings.ts shortHash); uppercasing is CSS-only, so
 * text assertions match the lowercase source.
 */
export function shortHash(hash: string): string {
	return `${hash.slice(0, 12)}...${hash.slice(-8)}`;
}

export const MAILPIT_URL = process.env.E2E_MAILPIT_URL ?? "http://127.0.0.1:8026";
