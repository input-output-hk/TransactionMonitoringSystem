import { toast } from "sonner";

/**
 * Copy `text` to the clipboard with toast feedback.
 *
 * Uses the modern async Clipboard API exclusively — requires a secure
 * context (HTTPS or localhost). Pass `{ silent: true }` to suppress the
 * toast when the caller already shows other feedback.
 */
export async function copyToClipboard(
	text: string,
	options: { label?: string; silent?: boolean } = {},
): Promise<boolean> {
	const { label = "Copied to clipboard", silent = false } = options;
	if (!text) return false;

	try {
		await navigator.clipboard.writeText(text);
		if (!silent) toast.success(label);
		return true;
	} catch (e) {
		console.error("Clipboard write failed:", e);
		if (!silent) toast.error("Copy failed");
		return false;
	}
}
