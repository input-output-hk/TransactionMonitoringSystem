/**
 * Chain-level panels for the attack-detail view: what moved, and what the
 * script actually received.
 *
 * The scored-alert endpoint carries only the score vector plus fee and output
 * count, so these read `/api/v1/transactions/{hash}` instead. That request is
 * made lazily by the caller, when the analyst opens this tab.
 *
 * Two honesty rules run through the whole file, both inherited from how the
 * backend models this data:
 *
 *  - `script_data_available: false` means the raw payload could not be
 *    recovered, so empty datum/redeemer lists mean "unknown", not "none". On a
 *    flagged transaction those are very different, and the difference is shown.
 *  - a resolved input value is a LOWER BOUND when enrichment could not resolve
 *    every parent UTxO, so the total is labelled rather than presented as exact.
 */
import { Fragment, useState } from "react";

import { DatumPayload } from "@/components/attack-detail/datum-tree";
import {
	Divider,
	KeyVal,
	Section,
	Stack,
	TwoCol,
} from "@/components/attack-detail/layout";
import { Badge } from "@/components/ui/badge";
import { EmptyText, ErrorText, LoadingText } from "@/components/ui/status-text";
import type {
	OutputDatum,
	TransactionDetail,
	TransactionRedeemer,
	TxInput,
	TxOutput,
} from "@/lib/api/transactions";
import { REDEEMER_INDEX_UNKNOWN } from "@/lib/api/transactions";
import { formatBytes } from "@/lib/utils/bytes";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { formatAdaExact } from "@/lib/utils/numbers";
import { shortHash } from "@/lib/utils/strings";
import { ArrowDownLeft, ArrowUpRight } from "lucide-react";

/** Address truncation inside the UTxO lists, which are narrow columns. */
const ADDR_HEAD = 12;
const ADDR_TAIL = 8;

/**
 * Asset-unit truncation. The policy id keeps a tail so two policies with the
 * same prefix stay distinguishable; the asset name is the human-readable half
 * and reads head-first, so it keeps none.
 */
const POLICY_HEAD = 8;
const POLICY_TAIL = 4;
const ASSET_NAME_HEAD = 10;
const ASSET_NAME_TAIL = 0;

/** A datum hash gets more room: it is what an analyst cross-checks elsewhere. */
const DATUM_HASH_HEAD = 16;
const DATUM_HASH_TAIL = 8;

/** ADA is shown to 2dp here: these are per-UTxO amounts, not aggregates. */
const ADA_DECIMALS = 2;

/**
 * Assets listed before the rest collapse behind a toggle.
 *
 * One mainnet consolidation transaction put 20 policies in a single output,
 * which pushed its three sibling outputs off the panel: the reader saw a wall of
 * token lines and could not tell there were only four outputs. Six keeps a
 * typical NFT bundle fully visible while capping the long tail.
 */
const ASSET_PREVIEW_COUNT = 6;

/**
 * CIP-67 asset-name label prefixes, as used by CIP-68 (4 bytes = 8 hex chars).
 * The bytes AFTER the label are the readable name, so decoding without stripping
 * them turns "PUGCHAMP" into mojibake. Values from CIP-67's registry.
 */
const CIP67_LABELS: Record<string, string> = {
	"000643b0": "CIP-68 (100) reference",
	"000de140": "CIP-68 (222) NFT",
	"0014df10": "CIP-68 (333) fungible",
	"001bc280": "CIP-68 (444) rich FT",
};
const CIP67_LABEL_HEX_LEN = 8;

/** Printable ASCII, the range an asset name may be shown as text (RFC 20). */
const ASCII_PRINTABLE_MIN = 0x20;
const ASCII_PRINTABLE_MAX = 0x7e;

/**
 * An asset name as text when its bytes are printable ASCII, else as hex.
 *
 * On-chain names are arbitrary bytes, and 42414e4b means nothing to an operator
 * while BANK is instantly recognisable, which is why every explorer decodes
 * them. A name with even one non-printable byte stays hex: partial decoding
 * would let a binary name smuggle control characters into the panel, and a half
 * decoded name is less trustworthy than an honest hex string.
 */
function decodeAssetName(hex: string): {
	text: string;
	decoded: boolean;
	label?: string;
} {
	if (!hex) return { text: "", decoded: false };
	const label = CIP67_LABELS[hex.slice(0, CIP67_LABEL_HEX_LEN).toLowerCase()];
	const body = label ? hex.slice(CIP67_LABEL_HEX_LEN) : hex;
	if (!body || body.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(body)) {
		return { text: hex, decoded: false, ...(label ? { label } : {}) };
	}
	let out = "";
	for (let i = 0; i < body.length; i += 2) {
		const byte = Number.parseInt(body.slice(i, i + 2), 16);
		if (byte < ASCII_PRINTABLE_MIN || byte > ASCII_PRINTABLE_MAX) {
			return { text: hex, decoded: false, ...(label ? { label } : {}) };
		}
		out += String.fromCharCode(byte);
	}
	return { text: out, decoded: true, ...(label ? { label } : {}) };
}

function AddressCell({ address }: { address: string }) {
	if (!address) {
		// An unresolved input has no address, which is a real state worth naming
		// rather than rendering as a blank cell.
		return (
			<span className="text-muted-foreground text-xs italic">unresolved</span>
		);
	}
	return (
		<button
			type="button"
			className="text-foreground hover:text-brand font-mono text-xs break-all"
			title={`${address} — click to copy`}
			onClick={() => void copyToClipboard(address, { label: "address" })}
		>
			{shortHash(address, ADDR_HEAD, ADDR_TAIL)}
		</button>
	);
}

/**
 * Native assets on one UTxO, indented under the output they belong to.
 *
 * The indent and the count header exist because without them the panel lied by
 * omission: an asset line looked exactly like an output line, so "Outputs (4)"
 * above 29 visually identical rows read as a wrong count rather than as four
 * outputs carrying 25 tokens. The list also collapses past
 * ASSET_PREVIEW_COUNT, so one fat output can no longer bury its siblings.
 */
function AssetLines({ assets }: { assets: Record<string, number> | null }) {
	const [expanded, setExpanded] = useState(false);
	const entries = Object.entries(assets ?? {});
	if (entries.length === 0) return null;
	const hidden = entries.length - ASSET_PREVIEW_COUNT;
	const shown = expanded ? entries : entries.slice(0, ASSET_PREVIEW_COUNT);
	return (
		// The left rule is the nesting cue: these belong to the row above.
		<div className="border-border/60 mt-1 space-y-0.5 border-l pl-2">
			<div className="text-muted-foreground text-[10px] tracking-wide uppercase">
				{entries.length === 1 ? "1 asset" : `${entries.length} assets`}
			</div>
			{shown.map(([unit, quantity]) => {
				// Stored as "policyId.assetNameHex". The policy stays truncated and the
				// full unit lives in the title, so a long unit cannot dominate the row.
				const dot = unit.indexOf(".");
				const policy = dot > 0 ? unit.slice(0, dot) : unit;
				const name = dot > 0 ? unit.slice(dot + 1) : "";
				const asset = decodeAssetName(name);
				const title = [unit, asset.label, asset.decoded ? `hex: ${name}` : null]
					.filter(Boolean)
					.join("\n");
				return (
					<div
						key={unit}
						className="text-muted-foreground flex items-baseline justify-between gap-2 text-[11px]"
						title={title}
					>
						<span className="break-all">
							<span className="font-mono">
								{shortHash(policy, POLICY_HEAD, POLICY_TAIL)}
							</span>
							{asset.text && (
								<>
									<span className="font-mono">.</span>
									{/* Decoded names are prose, so they drop the mono face; a
									    name that stayed hex keeps it. */}
									<span className={asset.decoded ? "" : "font-mono"}>
										{asset.decoded
											? asset.text
											: shortHash(asset.text, ASSET_NAME_HEAD, ASSET_NAME_TAIL)}
									</span>
								</>
							)}
						</span>
						<span className="shrink-0 tabular-nums">
							×{quantity.toLocaleString()}
						</span>
					</div>
				);
			})}
			{hidden > 0 && (
				<button
					type="button"
					className="text-muted-foreground hover:text-foreground text-[10px] underline underline-offset-2"
					onClick={(e) => {
						// The panel sits inside a clickable detail surface; keep the toggle
						// from doubling as a navigation.
						e.stopPropagation();
						setExpanded((v) => !v);
					}}
				>
					{expanded ? "show fewer" : `show all ${entries.length}`}
				</button>
			)}
		</div>
	);
}

function UtxoRow({
	address,
	amount,
	assets,
	flags,
}: {
	address: string;
	amount: number;
	assets: Record<string, number> | null;
	flags?: string[];
}) {
	return (
		<div className="py-1.5">
			<div className="flex items-baseline justify-between gap-3">
				<AddressCell address={address} />
				<span className="text-foreground shrink-0 text-sm tabular-nums">
					{formatAdaExact(amount, ADA_DECIMALS)} ADA
				</span>
			</div>
			{flags && flags.length > 0 && (
				<div className="mt-1 flex flex-wrap gap-1">
					{flags.map((f) => (
						<Badge
							key={f}
							variant="outline"
							className="text-muted-foreground border-border/60 text-[9px] font-normal tracking-normal normal-case"
						>
							{f}
						</Badge>
					))}
				</div>
			)}
			<AssetLines assets={assets} />
		</div>
	);
}

function inputFlags(input: TxInput): string[] {
	const flags: string[] = [];
	if (input.is_reference) flags.push("reference");
	if (input.is_collateral) flags.push("collateral");
	// A failed transaction's attempted spend: shown because it is what the
	// transaction TRIED to consume, even though flow analytics exclude it.
	if (input.is_unspent_attempt) flags.push("attempted spend");
	return flags;
}

function UtxoList<T>({
	items,
	render,
	empty,
}: {
	items: T[];
	render: (item: T, index: number) => React.ReactNode;
	empty: string;
}) {
	if (items.length === 0) return <EmptyText>{empty}</EmptyText>;
	return (
		<div>
			{items.map((item, i) => (
				<Fragment key={i}>
					{i > 0 && <Divider />}
					{render(item, i)}
				</Fragment>
			))}
		</div>
	);
}

/** Inputs and outputs with ADA and native assets: the "value transferred" view. */
export function ValueTransferredPanel({ tx }: { tx: TransactionDetail }) {
	const resolvedInputs = tx.inputs.filter((i) => !!i.address).length;
	const inputsIncomplete =
		tx.total_input_value === null || resolvedInputs < tx.inputs.length;

	return (
		<Section title="Value Transferred">
			<div className="mb-4 space-y-3">
				<KeyVal
					label="Total in"
					value={
						tx.total_input_value === null ? (
							<span className="text-muted-foreground text-xs italic">
								unresolved
							</span>
						) : (
							<span className="tabular-nums">
								{formatAdaExact(tx.total_input_value, ADA_DECIMALS)} ADA
								{inputsIncomplete && (
									<span className="text-muted-foreground"> (at least)</span>
								)}
							</span>
						)
					}
				/>
				<KeyVal
					label="Total out"
					value={
						<span className="tabular-nums">
							{formatAdaExact(tx.total_output_value, ADA_DECIMALS)} ADA
						</span>
					}
				/>
				<KeyVal
					label="Fee"
					value={
						<span className="tabular-nums">
							{formatAdaExact(tx.fee, ADA_DECIMALS)} ADA
						</span>
					}
				/>
			</div>
			{inputsIncomplete && (
				// Say this rather than showing a total that looks exact: input values
				// are resolved by a separate enrichment pass that can miss a parent
				// UTxO, and an analyst comparing in against out needs to know.
				<p className="text-muted-foreground mb-4 text-xs">
					Some input values could not be resolved, so the input total is a lower
					bound and will not balance against the outputs.
				</p>
			)}
			<TwoCol
				left={
					<Stack title={`Inputs (${tx.inputs.length})`}>
						<UtxoList
							items={tx.inputs}
							empty="No inputs recorded."
							render={(input: TxInput) => (
								<UtxoRow
									address={input.address}
									amount={input.amount}
									assets={input.assets}
									flags={inputFlags(input)}
								/>
							)}
						/>
					</Stack>
				}
				right={
					<Stack title={`Outputs (${tx.outputs.length})`}>
						<UtxoList
							items={tx.outputs}
							empty="No outputs recorded."
							render={(output: TxOutput) => (
								<UtxoRow
									address={output.address}
									amount={output.amount}
									assets={output.assets}
									flags={output.is_collateral ? ["collateral"] : []}
								/>
							)}
						/>
					</Stack>
				}
			/>
		</Section>
	);
}

function DatumEntry({ datum }: { datum: OutputDatum }) {
	return (
		<div className="space-y-2 py-2">
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-foreground text-sm font-semibold">
					Output #{datum.output_index}
				</span>
				<Badge
					variant="outline"
					className="text-muted-foreground border-border/60 text-[9px] font-normal tracking-normal normal-case"
					title={
						datum.resolved_from_witness
							? "The output carried only a datum hash; this payload is the preimage found in the transaction's witness set."
							: "The datum was attached to the output directly."
					}
				>
					{datum.resolved_from_witness ? "hash + witness preimage" : "inline"}
				</Badge>
				{datum.size_bytes !== null && (
					<span className="text-muted-foreground text-xs">
						{formatBytes(datum.size_bytes)}
					</span>
				)}
			</div>
			{datum.datum_hash && (
				<KeyVal
					label="Datum hash"
					value={
						<button
							type="button"
							className="text-foreground hover:text-brand font-mono text-xs break-all"
							title={`${datum.datum_hash} — click to copy`}
							onClick={() =>
								void copyToClipboard(datum.datum_hash as string, {
									label: "datum hash",
								})
							}
						>
							{shortHash(datum.datum_hash, DATUM_HASH_HEAD, DATUM_HASH_TAIL)}
						</button>
					}
				/>
			)}
			{datum.datum_hash && !datum.hex && !datum.structure?.root ? (
				// The hash is on chain but its preimage is not in this transaction, so
				// the content genuinely is not knowable from it.
				<p className="text-muted-foreground text-xs">
					The preimage is not present in this transaction, so the datum content
					cannot be shown.
				</p>
			) : (
				<DatumPayload hex={datum.hex} structure={datum.structure} />
			)}
		</div>
	);
}

function RedeemerEntry({ redeemer }: { redeemer: TransactionRedeemer }) {
	const hasIndex = redeemer.index !== REDEEMER_INDEX_UNKNOWN;
	return (
		<div className="space-y-2 py-2">
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-foreground text-sm font-semibold capitalize">
					{redeemer.purpose || "unknown purpose"}
				</span>
				{hasIndex && (
					<span className="text-muted-foreground font-mono text-xs">
						#{redeemer.index}
					</span>
				)}
				<span className="text-muted-foreground text-xs">
					{redeemer.memory_units.toLocaleString()} mem ·{" "}
					{redeemer.cpu_units.toLocaleString()} cpu
				</span>
			</div>
			<DatumPayload hex={redeemer.hex} structure={redeemer.structure} />
		</div>
	);
}

/** Datums and redeemers: the script-facing half of the transaction. */
export function ScriptDataPanel({ tx }: { tx: TransactionDetail }) {
	if (!tx.script_data_available) {
		return (
			<Section title="Datum & Redeemer">
				{/* Not "this transaction has none": the raw payload could not be
				    recovered, so the answer is unknown, and on a flagged transaction
				    that must not read as an absence of script data. */}
				<EmptyText>
					The raw transaction payload could not be recovered, so datum and
					redeemer data is unavailable for this transaction.
				</EmptyText>
			</Section>
		);
	}
	const hasNothing = tx.datums.length === 0 && tx.redeemers.length === 0;
	return (
		<Section title="Datum & Redeemer">
			{hasNothing ? (
				<EmptyText>
					This transaction carries no datums or redeemers (no Plutus script
					interaction).
				</EmptyText>
			) : (
				<div className="space-y-6">
					<Stack
						title={
							<>
								<ArrowDownLeft className="mr-1 inline h-3.5 w-3.5" />
								{`Datums (${tx.datums.length})`}
							</>
						}
					>
						<UtxoList
							items={tx.datums}
							empty="No output carries a datum."
							render={(datum: OutputDatum) => <DatumEntry datum={datum} />}
						/>
					</Stack>
					<Stack
						title={
							<>
								<ArrowUpRight className="mr-1 inline h-3.5 w-3.5" />
								{`Redeemers (${tx.redeemers.length})`}
							</>
						}
					>
						<UtxoList
							items={tx.redeemers}
							empty="No redeemers (no script was executed)."
							render={(redeemer: TransactionRedeemer) => (
								<RedeemerEntry redeemer={redeemer} />
							)}
						/>
					</Stack>
				</div>
			)}
		</Section>
	);
}

/** Loading / error / missing wrapper around the chain-detail panels. */
export function TransactionDetailPanels({
	tx,
	isPending,
	isError,
	error,
}: {
	tx: TransactionDetail | null | undefined;
	isPending: boolean;
	isError: boolean;
	error: unknown;
}) {
	if (isPending) {
		return (
			<Section title="Transaction Detail">
				<LoadingText>Loading transaction detail…</LoadingText>
			</Section>
		);
	}
	if (isError) {
		return (
			<Section title="Transaction Detail">
				<ErrorText>
					{`Failed to load transaction detail: ${
						error instanceof Error ? error.message : "unknown error"
					}`}
				</ErrorText>
			</Section>
		);
	}
	if (!tx) {
		return (
			<Section title="Transaction Detail">
				{/* A scored alert can outlive its transaction row under retention. */}
				<EmptyText>
					This transaction is no longer in the store, so its inputs, outputs and
					script data cannot be shown.
				</EmptyText>
			</Section>
		);
	}
	return (
		<>
			<ValueTransferredPanel tx={tx} />
			<Divider />
			<ScriptDataPanel tx={tx} />
		</>
	);
}
