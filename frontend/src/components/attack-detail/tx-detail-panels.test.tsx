/**
 * The chain-detail panels: value transferred, datums and redeemers.
 *
 * The load-bearing assertions are the honesty ones. An unrecoverable raw payload
 * must read as "unknown", never as "this transaction has no script data", and an
 * input total built from partially-resolved parents must be labelled a lower
 * bound rather than presented as exact.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
	ScriptDataPanel,
	TransactionDetailPanels,
	ValueTransferredPanel,
} from "@/components/attack-detail/tx-detail-panels";
import type { TransactionDetail } from "@/lib/api/transactions";

afterEach(cleanup);

const LOVELACE = 1_000_000;
const SCRIPT_ADDR = "addr_test1wq9scripttargetaddress000000000";
const PAYMENT_ADDR = "addr_test1qzpaymentaddress0000000000000000";

function tx(over: Partial<TransactionDetail> = {}): TransactionDetail {
	return {
		tx_hash: "a".repeat(64),
		slot: 1,
		block_height: 2,
		block_hash: "b".repeat(64),
		block_index: 0,
		timestamp: "2026-08-01T10:00:00Z",
		fee: LOVELACE / 2,
		deposit: null,
		input_count: 1,
		output_count: 1,
		total_input_value: 10 * LOVELACE,
		total_output_value: 9 * LOVELACE,
		addresses: [SCRIPT_ADDR],
		inputs: [
			{
				tx_hash: "c".repeat(64),
				index: 0,
				address: SCRIPT_ADDR,
				amount: 10 * LOVELACE,
				assets: null,
				is_reference: false,
				is_collateral: false,
				is_unspent_attempt: false,
			},
		],
		outputs: [
			{
				index: 0,
				address: PAYMENT_ADDR,
				amount: 9 * LOVELACE,
				assets: null,
				is_collateral: false,
			},
		],
		metadata: null,
		datums: [],
		redeemers: [],
		script_data_available: true,
		...over,
	};
}

describe("ValueTransferredPanel", () => {
	it("shows the ADA totals for both sides and the fee", () => {
		// The amount and its " ADA" suffix are separate JSX text nodes, so match on
		// the composed textContent of the leaf element rather than a single node.
		const amount = (text: string) =>
			screen.getAllByText(
				(_, el) => el?.tagName === "SPAN" && el.textContent === text,
			);
		render(<ValueTransferredPanel tx={tx()} />);
		// formatAdaExact caps fraction digits rather than padding them, so a whole
		// number of ADA renders without a decimal part.
		expect(amount("10 ADA").length).toBeGreaterThan(0);
		expect(amount("9 ADA").length).toBeGreaterThan(0);
		expect(amount("0.5 ADA").length).toBeGreaterThan(0);
	});

	it("counts inputs and outputs in the column headings", () => {
		render(<ValueTransferredPanel tx={tx()} />);
		expect(screen.getByText("Inputs (1)")).toBeInTheDocument();
		expect(screen.getByText("Outputs (1)")).toBeInTheDocument();
	});

	it("lists native assets per UTxO with their quantities", () => {
		const policy = "d".repeat(56);
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.484f534b59`]: 1234 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("×1,234")).toBeInTheDocument();
	});

	it("truncates a long HEX asset name instead of echoing it whole", () => {
		// The asset name is truncated head-only (it reads head-first and has no
		// meaningful suffix), which passes tail = 0 to shortHash. `slice(-0)` is
		// `slice(0)`, so that used to render head + "..." + the FULL name: longer
		// than the untruncated unit and reading as two assets run together. The
		// name here is 30 chars, past shortHash's break-even point; the previous
		// fixture's 10-char name was under it and could not catch this.
		//
		// The first byte is 0x01, deliberately non-printable: a printable name is
		// now decoded to text and shown in full, so only a name that STAYS hex
		// still exercises truncation.
		const policy = "d".repeat(56);
		const name = "01" + "61726461536e656b4e46543132";
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.${name}`]: 1 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		const rendered = screen.getByTitle(`${policy}.${name}`).textContent ?? "";
		expect(rendered).not.toContain(name);
		expect(rendered).toContain("0161726461...");
	});

	it("shows a printable asset name as text, keeping the hex in the title", () => {
		// 42414e4b is BANK. An operator recognises the ticker, not the bytes, which
		// is why every explorer decodes this.
		const policy = "d".repeat(56);
		const name = "42414e4b";
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.${name}`]: 5 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("BANK")).toBeInTheDocument();
		const row = screen.getByText("BANK").closest("div");
		expect(row?.getAttribute("title") ?? "").toContain(name);
	});

	it("keeps a non-printable asset name as hex rather than half-decoding it", () => {
		// One unprintable byte and the whole name stays hex: a partial decode would
		// let arbitrary bytes put control characters on screen, and a half-decoded
		// name is less trustworthy than an honest hex string.
		const policy = "d".repeat(56);
		const name = "4241004b";
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.${name}`]: 1 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		const rendered = screen.getByTitle(`${policy}.${name}`).textContent ?? "";
		expect(rendered).toContain(name);
		expect(screen.queryByText("BANK")).not.toBeInTheDocument();
	});

	it("strips a CIP-68 label before decoding the name under it", () => {
		// 0014df10 is CIP-67 label (333). Decoding the label bytes along with the
		// name yields mojibake, so the label is recognised and named instead.
		const policy = "d".repeat(56);
		const name = "0014df1042414e4b";
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.${name}`]: 7 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("BANK")).toBeInTheDocument();
		const title =
			screen.getByText("BANK").closest("div")?.getAttribute("title") ?? "";
		expect(title).toContain("CIP-68 (333)");
	});

	it("counts the assets on a UTxO and collapses a long list", () => {
		// Without the count and the collapse, one output holding 20 tokens rendered
		// 20 lines indistinguishable from output lines, so "Outputs (N)" read as a
		// wrong count and the sibling outputs were pushed off the panel.
		const assets: Record<string, number> = {};
		for (let i = 0; i < 9; i++) {
			assets[`${i.toString().repeat(56)}.42414e4b`] = i + 1;
		}
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets,
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("9 assets")).toBeInTheDocument();
		// Six shown, three behind the toggle.
		expect(screen.getAllByText("BANK")).toHaveLength(6);
		fireEvent.click(screen.getByText("show all 9"));
		expect(screen.getAllByText("BANK")).toHaveLength(9);
		fireEvent.click(screen.getByText("show fewer"));
		expect(screen.getAllByText("BANK")).toHaveLength(6);
	});

	it("singularises a single asset and offers no toggle", () => {
		const policy = "d".repeat(56);
		render(
			<ValueTransferredPanel
				tx={tx({
					outputs: [
						{
							index: 0,
							address: PAYMENT_ADDR,
							amount: LOVELACE,
							assets: { [`${policy}.42414e4b`]: 1 },
							is_collateral: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("1 asset")).toBeInTheDocument();
		expect(screen.queryByText(/show all/)).not.toBeInTheDocument();
	});

	it("labels the input total as a lower bound when a parent was unresolved", () => {
		// The values come from a separate enrichment pass that can miss a parent
		// UTxO, so a total that will not balance must say so.
		render(
			<ValueTransferredPanel
				tx={tx({
					input_count: 2,
					inputs: [
						...tx().inputs,
						{
							tx_hash: "e".repeat(64),
							index: 1,
							address: "",
							amount: 0,
							assets: null,
							is_reference: false,
							is_collateral: false,
							is_unspent_attempt: false,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText(/at least/i)).toBeInTheDocument();
		expect(screen.getByText(/lower\s+bound/i)).toBeInTheDocument();
		// The unresolved input is named rather than left as a blank row.
		expect(screen.getByText(/unresolved/i)).toBeInTheDocument();
	});

	it("reports a fully unresolved input total as unresolved, not as zero", () => {
		render(<ValueTransferredPanel tx={tx({ total_input_value: null })} />);
		expect(screen.getByText(/unresolved/i)).toBeInTheDocument();
	});

	it("flags reference, collateral and attempted-spend inputs", () => {
		render(
			<ValueTransferredPanel
				tx={tx({
					inputs: [
						{
							tx_hash: "f".repeat(64),
							index: 0,
							address: SCRIPT_ADDR,
							amount: LOVELACE,
							assets: null,
							is_reference: true,
							is_collateral: true,
							is_unspent_attempt: true,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("reference")).toBeInTheDocument();
		expect(screen.getByText("collateral")).toBeInTheDocument();
		expect(screen.getByText("attempted spend")).toBeInTheDocument();
	});
});

describe("ScriptDataPanel", () => {
	it("renders a datum's hash, delivery and decoded structure", () => {
		render(
			<ScriptDataPanel
				tx={tx({
					datums: [
						{
							output_index: 1,
							datum_hash: "9".repeat(64),
							resolved_from_witness: true,
							size_bytes: 42,
							hex: "d87980",
							structure: {
								root: {
									kind: "constructor",
									constructor_index: 0,
									value: null,
									text: null,
									children: [],
								},
								encoding: "cbor_hex",
								truncated: false,
								error: null,
							},
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("Output #1")).toBeInTheDocument();
		// Which delivery route the payload came from is a real distinction.
		expect(screen.getByText(/witness preimage/i)).toBeInTheDocument();
		expect(screen.getByText(/42B/)).toBeInTheDocument();
		expect(screen.getByText(/Constr 0/)).toBeInTheDocument();
	});

	it("labels an inline datum as inline", () => {
		render(
			<ScriptDataPanel
				tx={tx({
					datums: [
						{
							output_index: 0,
							datum_hash: null,
							resolved_from_witness: false,
							size_bytes: 3,
							hex: "d87980",
							structure: null,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("inline")).toBeInTheDocument();
	});

	it("says the content is unknowable when only a hash is on chain", () => {
		render(
			<ScriptDataPanel
				tx={tx({
					datums: [
						{
							output_index: 0,
							datum_hash: "9".repeat(64),
							resolved_from_witness: false,
							size_bytes: null,
							hex: null,
							structure: null,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText(/preimage is not present/i)).toBeInTheDocument();
	});

	it("renders a redeemer's purpose, index and execution budget", () => {
		render(
			<ScriptDataPanel
				tx={tx({
					redeemers: [
						{
							purpose: "spend",
							index: 2,
							hex: "d87980",
							structure: null,
							memory_units: 5000,
							cpu_units: 90000,
						},
					],
				})}
			/>,
		);
		expect(screen.getByText("spend")).toBeInTheDocument();
		expect(screen.getByText("#2")).toBeInTheDocument();
		expect(screen.getByText(/5,000 mem/)).toBeInTheDocument();
		expect(screen.getByText(/90,000 cpu/)).toBeInTheDocument();
	});

	it("omits the index when the payload did not state one", () => {
		render(
			<ScriptDataPanel
				tx={tx({
					redeemers: [
						{
							purpose: "mint",
							index: -1,
							hex: null,
							structure: null,
							memory_units: 0,
							cpu_units: 0,
						},
					],
				})}
			/>,
		);
		// -1 is the "not stated" sentinel and must never render as an index.
		expect(screen.queryByText("#-1")).not.toBeInTheDocument();
	});

	it("distinguishes 'no script data' from 'could not be recovered'", () => {
		// This is the important one: on a flagged transaction, absence of evidence
		// must not be presented as evidence of absence.
		const { unmount } = render(
			<ScriptDataPanel tx={tx({ script_data_available: false })} />,
		);
		expect(screen.getByText(/could not be recovered/i)).toBeInTheDocument();
		expect(
			screen.queryByText(/no datums or redeemers/i),
		).not.toBeInTheDocument();
		unmount();

		render(<ScriptDataPanel tx={tx({ script_data_available: true })} />);
		expect(screen.getByText(/no datums or redeemers/i)).toBeInTheDocument();
	});
});

describe("TransactionDetailPanels wrapper", () => {
	it("shows a loading state", () => {
		render(
			<TransactionDetailPanels
				tx={undefined}
				isPending
				isError={false}
				error={null}
			/>,
		);
		expect(screen.getByText(/Loading transaction detail/i)).toBeInTheDocument();
	});

	it("surfaces the error message", () => {
		render(
			<TransactionDetailPanels
				tx={undefined}
				isPending={false}
				isError
				error={new Error("boom")}
			/>,
		);
		expect(screen.getByText(/boom/)).toBeInTheDocument();
	});

	it("explains a missing transaction rather than rendering blank panels", () => {
		// A scored alert can outlive its transaction row under retention.
		render(
			<TransactionDetailPanels
				tx={null}
				isPending={false}
				isError={false}
				error={null}
			/>,
		);
		expect(screen.getByText(/no longer in the store/i)).toBeInTheDocument();
	});

	it("renders both panels once the transaction loads", () => {
		render(
			<TransactionDetailPanels
				tx={tx()}
				isPending={false}
				isError={false}
				error={null}
			/>,
		);
		expect(screen.getByText("Value Transferred")).toBeInTheDocument();
		expect(screen.getByText("Datum & Redeemer")).toBeInTheDocument();
	});
});
