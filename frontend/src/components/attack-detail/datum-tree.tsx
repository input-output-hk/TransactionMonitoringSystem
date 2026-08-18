/**
 * Renderer for a decoded Plutus datum or redeemer.
 *
 * The backend decodes the payload into a typed tree (constructor tags, field
 * order, map entries, leaf types) rather than a blob of hex, because "what does
 * this datum say" is the question an analyst opens a flagged transaction to
 * answer. This draws that tree, and keeps two distinctions the backend was
 * careful to preserve:
 *
 *  - a `truncated` node marks a subtree the depth or node budget elided. Showing
 *    it as an empty structure would claim there is nothing there, which is a
 *    different statement from "we stopped looking".
 *  - an `error` with no root means the payload could not be decoded. That is
 *    information about a flagged transaction, so it is stated, not hidden.
 */
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import type { DatumNode, DecodedDatum } from "@/lib/api/transactions";
import { cn } from "@/lib/utils";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { Check, ChevronDown, ChevronRight, Copy } from "lucide-react";

/** Indent per nesting level. Deep datums stay readable without a horizontal scroll. */
const INDENT_REM = 0.85;

/** Hex is monospace and wraps: a datum leaf can be kilobytes of it. */
const HEX_CLS = "font-mono text-[11px] break-all text-muted-foreground";

/** Label shown for each node kind. Structural kinds get a type word. */
const KIND_LABEL: Record<DatumNode["kind"], string> = {
	constructor: "Constr",
	list: "List",
	map: "Map",
	map_entry: "Entry",
	bytes: "Bytes",
	int: "Int",
	text: "Text",
	truncated: "Truncated",
	unknown: "Unknown",
};

function NodeRow({ node, depth }: { node: DatumNode; depth: number }) {
	const hasChildren = node.children.length > 0;
	const [open, setOpen] = useState(true);
	const Chevron = open ? ChevronDown : ChevronRight;

	return (
		<div style={{ paddingLeft: `${depth * INDENT_REM}rem` }}>
			<div className="flex items-start gap-1.5 py-0.5">
				{hasChildren ? (
					<button
						type="button"
						onClick={() => setOpen((v) => !v)}
						className="text-muted-foreground hover:text-foreground mt-0.5 shrink-0"
						aria-label={open ? "Collapse" : "Expand"}
						aria-expanded={open}
					>
						<Chevron className="h-3 w-3" />
					</button>
				) : (
					<span className="w-3 shrink-0" />
				)}
				<span
					className={cn(
						"mt-px shrink-0 text-[10px] font-semibold tracking-wide uppercase",
						node.kind === "truncated"
							? "text-severity-high-foreground"
							: "text-muted-foreground/70",
					)}
				>
					{KIND_LABEL[node.kind]}
					{node.kind === "constructor" && node.constructor_index !== null
						? ` ${node.constructor_index}`
						: ""}
				</span>
				{node.kind === "truncated" && (
					<span className="text-muted-foreground text-xs">
						not shown (payload exceeds the decode limit)
					</span>
				)}
				{node.value !== null && node.kind !== "truncated" && (
					<span className={node.kind === "int" ? "font-mono text-xs" : HEX_CLS}>
						{node.value}
					</span>
				)}
				{/* The UTF-8 reading of a byte leaf, which is where token names, labels
				    and URLs actually live. Quoted so it is not mistaken for a type. */}
				{node.text && (
					<span className="text-foreground text-xs break-all">
						“{node.text}”
					</span>
				)}
			</div>
			{open &&
				hasChildren &&
				node.children.map((child, i) => (
					<NodeRow key={i} node={child} depth={depth + 1} />
				))}
		</div>
	);
}

function CopyButton({ text, label }: { text: string; label: string }) {
	const [copied, setCopied] = useState(false);
	return (
		<button
			type="button"
			className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
			onClick={() => {
				void copyToClipboard(text, { label });
				setCopied(true);
				window.setTimeout(() => setCopied(false), 1200);
			}}
		>
			{copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
			{copied ? "Copied" : "Copy"}
		</button>
	);
}

/**
 * A payload with a raw view and a decoded view.
 *
 * Both are offered because they answer different questions: the hex is what is
 * on chain and what an analyst pastes into another tool, the tree is what it
 * means. Defaults to the decoded view when there is one.
 */
export function DatumPayload({
	hex,
	structure,
	hexLabel = "Raw CBOR (hex)",
}: {
	hex: string | null;
	structure: DecodedDatum | null;
	hexLabel?: string;
}) {
	const decodable = !!structure?.root;
	const [view, setView] = useState<"decoded" | "raw">(
		decodable ? "decoded" : "raw",
	);

	if (!hex && !decodable) {
		return (
			<p className="text-muted-foreground text-xs">
				{structure?.error
					? `Could not decode: ${structure.error}.`
					: "No payload."}
			</p>
		);
	}

	return (
		<div className="space-y-2">
			<div className="flex items-center gap-3">
				{decodable && hex && (
					<div className="flex items-center gap-1">
						{(["decoded", "raw"] as const).map((v) => (
							<button
								key={v}
								type="button"
								onClick={() => setView(v)}
								className={cn(
									"rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase",
									view === v
										? "bg-secondary text-secondary-foreground"
										: "text-muted-foreground hover:text-foreground",
								)}
							>
								{v === "decoded" ? "Structure" : "Hex"}
							</button>
						))}
					</div>
				)}
				{hex && <CopyButton text={hex} label="payload" />}
				{structure?.truncated && (
					<Badge
						variant="outline"
						className="text-muted-foreground border-border/60 text-[9px] font-normal tracking-normal normal-case"
						title="The payload exceeded the decode limits, so part of the structure is not shown. The raw hex is complete."
					>
						partially decoded
					</Badge>
				)}
			</div>

			{structure?.error && (
				<p className="text-muted-foreground text-xs">
					{`Could not decode: ${structure.error}.`}
				</p>
			)}

			{view === "decoded" && structure?.root ? (
				<div className="border-border bg-background/40 max-h-72 overflow-auto rounded border p-2">
					<NodeRow node={structure.root} depth={0} />
				</div>
			) : (
				hex && (
					<div className="border-border bg-background/40 max-h-48 overflow-auto rounded border p-2">
						<div className="text-muted-foreground/70 mb-1 text-[10px] font-semibold tracking-wide uppercase">
							{hexLabel}
						</div>
						<code className={HEX_CLS}>{hex}</code>
					</div>
				)
			)}
		</div>
	);
}
