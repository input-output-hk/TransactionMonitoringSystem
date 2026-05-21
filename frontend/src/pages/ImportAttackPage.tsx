import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CloudUpload, X } from "lucide-react";
import Papa from "papaparse";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { type ArchiveBulkEntry } from "@/lib/api/archive";
import { getNetwork } from "@/lib/api/fetch";
import { useBulkImportMutation } from "@/lib/archive-store";
import { cn } from "@/lib/utils";
import { formatBytes } from "@/lib/utils/bytes";
import { isCsv } from "@/lib/utils/mime";

type Phase = "idle" | "parsing" | "parsed" | "confirming" | "uploading";

type InvalidRow = { row: number; reason: string };
type ParseResult = {
	valid: ArchiveBulkEntry[];
	invalid: InvalidRow[];
};

export function ImportAttackPage() {
	const navigate = useNavigate();
	const inputRef = useRef<HTMLInputElement>(null);
	const [phase, setPhase] = useState<Phase>("idle");
	const [file, setFile] = useState<File | null>(null);
	const [parsed, setParsed] = useState<ParseResult | null>(null);
	const [dragOver, setDragOver] = useState(false);
	// Bumped on each new selection so the CSS animation restarts via `key`.
	const [selectionId, setSelectionId] = useState(0);
	const { mutateAsync: bulkImport } = useBulkImportMutation();

	const handleFile = (f: File | null) => {
		if (!f) return;
		if (!isCsv(f)) {
			toast.error("Invalid file. Please upload a valid CSV.");
			return;
		}
		setFile(f);
		setParsed(null);
		setPhase("parsing");
		setSelectionId((n) => n + 1);

		Papa.parse<Record<string, string>>(f, {
			header: true,
			skipEmptyLines: true,
			complete: (results) => {
				const result = transformRows(results.data);
				setParsed(result);
				setPhase("parsed");
				if (result.valid.length === 0 && result.invalid.length === 0) {
					toast.warning("CSV is empty.");
				} else if (result.invalid.length > 0) {
					toast.warning(
						`Parsed ${result.valid.length.toLocaleString()} rows · ${result.invalid.length.toLocaleString()} invalid skipped.`,
					);
				}
			},
			error: (err) => {
				console.error(err);
				toast.error("Failed to parse CSV.");
				setPhase("idle");
				setFile(null);
			},
		});
	};

	const onDrop = (e: React.DragEvent) => {
		e.preventDefault();
		setDragOver(false);
		const f = e.dataTransfer.files?.[0];
		if (f) handleFile(f);
	};

	const reset = () => {
		setPhase("idle");
		setFile(null);
		setParsed(null);
	};

	const onUploadClick = () => {
		if (!parsed || parsed.valid.length === 0) return;
		setPhase("confirming");
	};

	const onConfirm = async () => {
		if (!parsed) return;
		setPhase("uploading");
		try {
			// `source_label` tags the origin of imported rows on the backend
			// (`source = "import:<label>"`). Constant for now — could be made
			// configurable per-team if curation across many instances grows.
			// The hook invalidates the archive list + analysis queries on
			// success so the destination `/archive` page renders fresh data.
			const result = await bulkImport({
				entries: parsed.valid,
				sourceLabel: "frontend-csv",
			});
			const summary = [
				result.inserted > 0 && `${result.inserted} new`,
				result.skipped > 0 && `${result.skipped} skipped`,
			]
				.filter(Boolean)
				.join(" · ");
			toast.success(`Import complete. ${summary || "no changes"}.`);
			reset();
			navigate("/archive");
		} catch (e) {
			console.error(e);
			toast.error("Import failed. Please try again.");
			setPhase("parsed");
		}
	};

	const close = () => navigate("/archive");

	const validCount = parsed?.valid.length ?? 0;
	const invalidCount = parsed?.invalid.length ?? 0;
	const canUpload = phase === "parsed" && validCount > 0;

	return (
		<div className="border-border bg-card relative rounded-lg border-2 p-8 md:p-12">
			<button
				type="button"
				onClick={close}
				className="text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-ring absolute top-4 right-4 rounded-md p-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
				title="Close"
			>
				<X className="h-4 w-4" />
			</button>

			<div
				onDragOver={(e) => {
					e.preventDefault();
					setDragOver(true);
				}}
				onDragLeave={() => setDragOver(false)}
				onDrop={onDrop}
				className={cn(
					"flex min-h-[460px] flex-col items-center justify-center rounded-md border-2 border-dashed px-6 py-16 transition-colors",
					dragOver
						? "border-brand bg-brand/5"
						: "border-muted-foreground/40 bg-transparent",
				)}
			>
				<CloudUpload
					className="text-foreground mb-5 h-20 w-20"
					strokeWidth={0.75}
				/>
				<p className="text-foreground text-sm">
					Select a file or drag and drop here
				</p>
				<p className="text-muted-foreground mt-1 text-xs">
					CSV with non-attack alerts to merge into the archive
				</p>
				<Button
					type="button"
					variant="outline"
					className="mt-8"
					onClick={() => inputRef.current?.click()}
				>
					Select a file
				</Button>
				<input
					ref={inputRef}
					type="file"
					accept=".csv"
					className="hidden"
					onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
				/>
			</div>

			{file && (
				<div className="border-border mt-6 rounded-md border px-4 py-3">
					<div className="text-foreground flex items-center justify-between text-sm">
						<span className="truncate">{file.name}</span>
						<span className="text-muted-foreground text-xs">
							{formatBytes(file.size)}
						</span>
					</div>
					<div className="bg-muted mt-2 h-1 w-full overflow-hidden rounded-full">
						<div
							key={selectionId}
							className="bg-brand animate-progress-fill h-full"
						/>
					</div>
					{phase === "parsed" && (
						<div className="text-muted-foreground mt-2 text-xs">
							{validCount.toLocaleString()} valid
							{invalidCount > 0 && (
								<>
									{" · "}
									<span className="text-status-warning">
										{invalidCount.toLocaleString()} invalid (will be skipped)
									</span>
								</>
							)}
						</div>
					)}
				</div>
			)}

			<div className="mt-6 flex justify-end gap-2">
				{file && phase !== "uploading" && (
					<Button type="button" variant="outline" onClick={reset}>
						Clear
					</Button>
				)}
				<Button
					type="button"
					variant="outline"
					disabled={!canUpload}
					onClick={onUploadClick}
				>
					{phase === "uploading" ? "Uploading…" : "Upload"}
				</Button>
			</div>

			<Dialog
				open={phase === "confirming"}
				onOpenChange={(v) => !v && setPhase("parsed")}
			>
				<DialogContent showClose={false} className="max-w-sm">
					<DialogHeader>
						<DialogTitle>
							Import {validCount.toLocaleString()} non-attacks?
						</DialogTitle>
						<DialogDescription>
							Existing archive entries with the same tx_hash will be updated
							(last-write-wins). Active alerts referenced here will disappear
							from the dashboard once archived.
						</DialogDescription>
					</DialogHeader>
					<DialogFooter>
						<Button variant="outline" onClick={() => setPhase("parsed")}>
							Cancel
						</Button>
						<Button
							onClick={onConfirm}
							className="border-border text-brand hover:bg-accent hover:text-brand border bg-transparent"
						>
							Confirm
						</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>
		</div>
	);
}

/* ---------- CSV → ArchiveBulkEntry ---------- */

const VALID_NETWORKS = new Set(["mainnet", "preprod", "preview"]);

/**
 * Map one CSV row (header-driven) to an {@link ArchiveBulkEntry}. Columns
 * match the backend export format: `network, tx_hash, note, archived_by,
 * archived_at, source`. Permissive validation: only `tx_hash` is required;
 * everything else gets a sensible default.
 */
function transformRows(rows: Record<string, string>[]): ParseResult {
	const valid: ArchiveBulkEntry[] = [];
	const invalid: InvalidRow[] = [];

	rows.forEach((r, i) => {
		const lineNo = i + 2; // +1 for header, +1 for 1-based
		const tx_hash = (r.tx_hash ?? "").trim();
		if (!tx_hash) {
			invalid.push({ row: lineNo, reason: "missing tx_hash" });
			return;
		}
		const rawNetwork = (r.network ?? "").trim().toLowerCase();
		const network = (
			VALID_NETWORKS.has(rawNetwork) ? rawNetwork : getNetwork()
		) as ArchiveBulkEntry["network"];
		const archivedAt = (r.archived_at ?? "").trim();
		valid.push({
			network,
			tx_hash,
			note: (r.note ?? "").trim() || "Imported",
			archived_by: (r.archived_by ?? "").trim() || "Unknown",
			archived_at: archivedAt || undefined,
			source: (r.source ?? "").trim() || undefined,
		});
	});

	return { valid, invalid };
}
