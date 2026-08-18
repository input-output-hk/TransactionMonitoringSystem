/**
 * The decoded datum / redeemer renderer.
 *
 * The distinctions under test are the ones the backend deliberately preserves
 * and the UI must not flatten: truncated is not empty, undecodable is not empty,
 * and a byte leaf's UTF-8 reading is shown alongside its hex rather than instead
 * of it.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DatumPayload } from "@/components/attack-detail/datum-tree";
import type { DatumNode, DecodedDatum } from "@/lib/api/transactions";

afterEach(cleanup);

function node(over: Partial<DatumNode> = {}): DatumNode {
	return {
		kind: "bytes",
		constructor_index: null,
		value: null,
		text: null,
		children: [],
		...over,
	};
}

function decoded(root: DatumNode | null, over: Partial<DecodedDatum> = {}) {
	return {
		root,
		encoding: "cbor_hex" as const,
		truncated: false,
		error: null,
		...over,
	};
}

const JPG_STORE_HEX = "6a70672e73746f7265";

describe("DatumPayload structure view", () => {
	it("renders a constructor with its index and field leaves", () => {
		render(
			<DatumPayload
				hex="d8799f..."
				structure={decoded(
					node({
						kind: "constructor",
						constructor_index: 0,
						children: [
							node({ kind: "bytes", value: JPG_STORE_HEX, text: "jpg.store" }),
							node({ kind: "int", value: "42" }),
						],
					}),
				)}
			/>,
		);
		expect(screen.getByText(/Constr 0/)).toBeInTheDocument();
		// Both readings of the byte leaf: the hex is what is on chain, the text is
		// what it means, and token names and URLs ride in byte strings.
		expect(screen.getByText(JPG_STORE_HEX)).toBeInTheDocument();
		expect(screen.getByText(/jpg\.store/)).toBeInTheDocument();
		expect(screen.getByText("42")).toBeInTheDocument();
	});

	it("marks an elided subtree rather than showing it as empty", () => {
		render(
			<DatumPayload
				hex="ff"
				structure={decoded(
					node({ kind: "list", children: [node({ kind: "truncated" })] }),
					{ truncated: true },
				)}
			/>,
		);
		// "We stopped looking" must not read as "there is nothing here".
		expect(screen.getByText(/exceeds the decode limit/i)).toBeInTheDocument();
		expect(screen.getByText(/partially decoded/i)).toBeInTheDocument();
	});

	it("renders nested map entries", () => {
		render(
			<DatumPayload
				hex="a1"
				structure={decoded(
					node({
						kind: "map",
						children: [
							node({
								kind: "map_entry",
								children: [
									node({ kind: "bytes", value: "61", text: "a" }),
									node({ kind: "int", value: "7" }),
								],
							}),
						],
					}),
				)}
			/>,
		);
		expect(screen.getByText(/Entry/)).toBeInTheDocument();
		expect(screen.getByText("7")).toBeInTheDocument();
	});

	it("collapses and re-expands a subtree", () => {
		render(
			<DatumPayload
				hex="9f"
				structure={decoded(
					node({
						kind: "list",
						children: [node({ kind: "int", value: "99" })],
					}),
				)}
			/>,
		);
		expect(screen.getByText("99")).toBeInTheDocument();
		fireEvent.click(screen.getByLabelText("Collapse"));
		expect(screen.queryByText("99")).not.toBeInTheDocument();
		fireEvent.click(screen.getByLabelText("Expand"));
		expect(screen.getByText("99")).toBeInTheDocument();
	});
});

describe("DatumPayload raw view", () => {
	it("switches between the structure and the hex", () => {
		render(
			<DatumPayload
				hex={JPG_STORE_HEX}
				structure={decoded(node({ kind: "int", value: "1" }))}
			/>,
		);
		// Defaults to the decoded view when there is one.
		expect(screen.getByText("1")).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: /hex/i }));
		expect(screen.getByText(/Raw CBOR/i)).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: /structure/i }));
		expect(screen.getByText("1")).toBeInTheDocument();
	});

	it("falls back to the hex when the payload could not be decoded", () => {
		render(
			<DatumPayload
				hex="zzzz"
				structure={decoded(null, { error: "datum is not valid CBOR" })}
			/>,
		);
		// Undecodable is information about a flagged transaction, so it is stated.
		expect(screen.getByText(/not valid CBOR/)).toBeInTheDocument();
		expect(screen.getByText("zzzz")).toBeInTheDocument();
	});

	it("offers a copy control for the raw payload", () => {
		render(<DatumPayload hex={JPG_STORE_HEX} structure={null} />);
		expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
	});
});

describe("DatumPayload empty states", () => {
	it("reports no payload when there is neither hex nor structure", () => {
		render(<DatumPayload hex={null} structure={null} />);
		expect(screen.getByText(/No payload/i)).toBeInTheDocument();
	});

	it("reports the decode error even with no hex to fall back on", () => {
		render(
			<DatumPayload
				hex={null}
				structure={decoded(null, { error: "datum is not valid hex" })}
			/>,
		);
		expect(screen.getByText(/not valid hex/)).toBeInTheDocument();
	});
});
