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
import { Fragment } from "react";

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

/** ADA is shown to 2dp here: these are per-UTxO amounts, not aggregates. */
const ADA_DECIMALS = 2;

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

/** Native assets on one UTxO, as `policy.name × quantity` rows. */
function AssetLines({ assets }: { assets: Record<string, number> | null }) {
	const entries = Object.entries(assets ?? {});
	if (entries.length === 0) return null;
	return (
		<div className="mt-1 space-y-0.5">
			{entries.map(([unit, quantity]) => {
				// Stored as "policyId.assetNameHex"; show the name and keep the policy
				// in the title so a long unit does not dominate the row.
				const dot = unit.indexOf(".");
				const policy = dot > 0 ? unit.slice(0, dot) : unit;
				const name = dot > 0 ? unit.slice(dot + 1) : "";
				return (
					<div
						key={unit}
						className="text-muted-foreground flex items-baseline justify-between gap-2 font-mono text-[11px]"
						title={unit}
					>
						<span className="break-all">
							{shortHash(policy, 8, 4)}
							{name && `.${shortHash(name, 10, 0)}`}
						</span>
						<span className="tabular-nums">×{quantity.toLocaleString()}</span>
					</div>
				);
			})}
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
							{shortHash(datum.datum_hash, 16, 8)}
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
