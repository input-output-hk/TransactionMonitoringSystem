import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { CloudUpload, X } from "lucide-react";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

type Phase = "idle" | "selected" | "confirming" | "uploading";

export function ImportAttackPage() {
	const navigate = useNavigate();
	const inputRef = useRef<HTMLInputElement>(null);
	const [phase, setPhase] = useState<Phase>("idle");
	const [file, setFile] = useState<File | null>(null);
	const [dragOver, setDragOver] = useState(false);
	// Bumped on each new selection so the CSS animation restarts via `key`.
	const [selectionId, setSelectionId] = useState(0);

	const handleFile = (f: File | null) => {
		if (!f) return;
		if (!isCsv(f)) {
			toast.error("Invalid file. Please upload a valid CSV.");
			return;
		}
		setFile(f);
		setPhase("selected");
		setSelectionId((n) => n + 1);
	};

	const onDrop = (e: React.DragEvent) => {
		e.preventDefault();
		setDragOver(false);
		const f = e.dataTransfer.files?.[0];
		if (f) handleFile(f);
	};

	const onUploadClick = () => setPhase("confirming");

	const onConfirm = async () => {
		setPhase("uploading");

		await new Promise((r) => setTimeout(r, 800));
		const ok = Math.random() < 0.7;
		if (ok) {
			toast.success("Upload successfull.");
			setFile(null);
			setPhase("idle");
			navigate("/dashboard");
		} else {
			toast.error("Upload unsuccessful. Please try again.");
			setPhase("selected");
		}
	};

	const close = () => navigate("/dashboard");

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
				<p className="text-muted-foreground mt-1 text-xs">CSV file</p>
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
				</div>
			)}

			<div className="mt-6 flex justify-end">
				<Button
					type="button"
					variant="outline"
					disabled={!file || phase === "uploading"}
					onClick={onUploadClick}
				>
					{phase === "uploading" ? "Uploading…" : "Upload"}
				</Button>
			</div>

			<Dialog
				open={phase === "confirming"}
				onOpenChange={(v) => !v && setPhase("selected")}
			>
				<DialogContent showClose={false} className="max-w-sm">
					<DialogHeader>
						<DialogTitle>
							Are you sure you want to import this file?
						</DialogTitle>
					</DialogHeader>
					<DialogFooter>
						<Button variant="outline" onClick={() => setPhase("selected")}>
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

function formatBytes(bytes: number): string {
	if (bytes < 1024) return `${bytes}B`;
	if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
	return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

const CSV_MIME_TYPES = new Set([
	"text/csv",
	"application/csv",
	"application/vnd.ms-excel", // browser quirk: some OSes label .csv this way
	"", // some browsers leave it blank — fall back to the extension check
]);

function isCsv(file: File): boolean {
	const hasCsvExt = file.name.toLowerCase().endsWith(".csv");
	return hasCsvExt && CSV_MIME_TYPES.has(file.type);
}
