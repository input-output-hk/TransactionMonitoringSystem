/** Known MIME types that browsers/OSes use for CSV files. */
export const CSV_MIME_TYPES = new Set([
	"text/csv",
	"application/csv",
	// Browser quirk: some OSes label .csv this way (Excel association)
	"application/vnd.ms-excel",
	// Some browsers leave it blank — fall back to the extension check
	"",
]);

/** Lenient CSV check: extension `.csv` AND a recognized (or blank) MIME type. */
export function isCsv(file: File): boolean {
	const hasCsvExt = file.name.toLowerCase().endsWith(".csv");
	return hasCsvExt && CSV_MIME_TYPES.has(file.type);
}
