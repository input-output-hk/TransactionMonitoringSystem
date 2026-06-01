import Papa from "papaparse";

/**
 * Serialize `rows` to CSV via PapaParse and trigger a browser download.
 *
 * - First row's keys become the header.
 * - Prepends a UTF-8 BOM so Excel opens non-ASCII correctly.
 * - Caller is responsible for ensuring `rows` are flat, CSV-friendly objects
 *   (nested arrays/objects get JSON-stringified by Papa, which is rarely
 *   what you want — pre-flatten upstream).
 */
export function downloadCsv<T extends Record<string, unknown>>(
	rows: T[],
	filename: string,
): void {
	const csv = Papa.unparse(rows, { header: true });
	const blob = new Blob(["﻿" + csv], {
		type: "text/csv;charset=utf-8;",
	});
	const url = URL.createObjectURL(blob);
	const a = document.createElement("a");
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	a.remove();
	URL.revokeObjectURL(url);
}
