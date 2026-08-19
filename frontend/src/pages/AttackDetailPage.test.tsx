/**
 * The attack-detail evidence section.
 *
 * The switch over attack types had no `Contract Anomaly` arm and no `default`, so
 * that class rendered nothing between two dividers: an empty card for the
 * highest-volume class, i.e. the one most likely to be opened. These tests pin
 * the arm and the fallback that closes that hole for any future class.
 */
import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { RiskAlert } from "@/lib/attacks";

const TARGET = "addr_test1wq9anomalytargetaddress000000";

const state: { alert: RiskAlert | null } = { alert: null };

vi.mock("@/lib/api/analysis", async (importOriginal) => {
	const actual = await importOriginal<typeof import("@/lib/api/analysis")>();
	return {
		...actual,
		useRiskAlert: () => ({
			data: state.alert,
			isPending: false,
			isError: false,
			error: null,
		}),
	};
});

vi.mock("@/lib/api/transactions", () => ({
	useTransactionDetail: () => ({
		data: null,
		isPending: false,
		isError: false,
		error: null,
	}),
}));

// The detail card reads the signed-in user for the archive audit trail. Mocked
// rather than wrapping in a real AuthProvider, which would pull in the /me fetch.
vi.mock("@/lib/auth", () => ({
	useAuth: () => ({
		user: { email: "reviewer@example.com", role: "Admin" },
		isAuthenticated: true,
		isAdmin: true,
		isLoading: false,
		isReady: true,
		isError: false,
		logout: vi.fn(),
		refetchUser: vi.fn(),
	}),
}));

vi.mock("@/lib/archive-store", () => ({
	useIsArchived: () => false,
	useArchiveMeta: () => ({ data: null }),
	useArchiveMutation: () => ({ mutate: vi.fn(), isPending: false }),
	useRestoreMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));

function baseAlert(over: Partial<RiskAlert> = {}): RiskAlert {
	const hash = "a".repeat(64);
	return {
		slug: hash,
		id: hash.slice(0, 12),
		fullHash: hash,
		date: "01.08.2026, 10:00 UTC",
		attackType: "Contract Anomaly",
		severity: "MODERATE",
		riskScore: 45,
		feeAda: 0.5,
		outputs: 2,
		...over,
	};
}

/** Reports the live URL so a test can assert what a navigation preserved. */
function LocationProbe() {
	const { pathname, search } = useLocation();
	return <div data-testid="location">{`${pathname}${search}`}</div>;
}

async function renderDetail(entry = `/attacks/${"a".repeat(64)}`) {
	const { AttackDetailPage } = await import("@/pages/AttackDetailPage");
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<MemoryRouter initialEntries={[entry]}>
			<QueryClientProvider client={client}>
				<TooltipProvider>
					<LocationProbe />
					<AttackDetailPage />
				</TooltipProvider>
			</QueryClientProvider>
		</MemoryRouter>,
	);
}

afterEach(() => {
	state.alert = null;
	cleanup();
});

describe("panel order", () => {
	it("puts the sub-scores above the chain-level transaction detail", async () => {
		// The transaction panels run to dozens of lines on a multi-asset
		// transaction, so anything below them is effectively hidden. The sub-scores
		// explain the score the whole modal is about and have to come first.
		state.alert = baseAlert();
		await renderDetail();
		const body = document.body.textContent ?? "";
		const subScores = body.indexOf("Sub-scores");
		// With the transaction mocked as absent, the panels render their
		// missing-transaction message; its position is the panels' position.
		const txPanels = body.indexOf("no longer in the store");
		expect(subScores).toBeGreaterThan(-1);
		expect(txPanels).toBeGreaterThan(-1);
		expect(subScores).toBeLessThan(txPanels);
	});
});

describe("Contract Anomaly evidence", () => {
	it("renders the flagged contract and the model that flagged it", async () => {
		state.alert = baseAlert({
			evidence: {
				target: TARGET,
				model_id: "iso-2026-07",
				feature_set: "shape",
				cluster_id: 3,
				consensus: 0.82,
				votes: 2,
				verdict: "anomaly",
			},
		});
		await renderDetail();
		expect(screen.getByText("Flagged Contract")).toBeInTheDocument();
		expect(screen.getByText("iso-2026-07")).toBeInTheDocument();
		expect(screen.getByText("shape")).toBeInTheDocument();
		expect(screen.getByText("#3")).toBeInTheDocument();
		expect(screen.getByText("82%")).toBeInTheDocument();
		expect(screen.getByText("anomaly")).toBeInTheDocument();
	});

	it("links the target to its validator page", async () => {
		state.alert = baseAlert({ evidence: { target: TARGET } });
		await renderDetail();
		const link = screen.getByTitle(TARGET);
		expect(link).toHaveAttribute("href", `/validators/${TARGET}`);
	});

	it("names DBSCAN's noise label rather than showing it as cluster -1", async () => {
		state.alert = baseAlert({ evidence: { target: TARGET, cluster_id: -1 } });
		await renderDetail();
		expect(screen.getByText(/noise \(no cluster\)/i)).toBeInTheDocument();
		expect(screen.queryByText("#-1")).not.toBeInTheDocument();
	});

	it("explains an un-clusterable fit", async () => {
		state.alert = baseAlert({
			evidence: { target: TARGET },
			unclusterableModel: true,
		});
		await renderDetail();
		expect(screen.getByText(/structural noise/i)).toBeInTheDocument();
	});

	it("renders the section even with empty evidence", async () => {
		// The bug being fixed: this used to produce two adjacent dividers and
		// nothing between them.
		state.alert = baseAlert({ evidence: {} });
		await renderDetail();
		expect(screen.getByText("Flagged Contract")).toBeInTheDocument();
	});
});

describe("unknown attack class fallback", () => {
	it("dumps raw evidence rather than rendering an empty panel", async () => {
		// A class the UI has no bespoke panel for must never be silently invisible.
		state.alert = baseAlert({
			// Simulates the title-case fallback for a backend class this build
			// predates.
			attackType: "Some Future Class" as RiskAlert["attackType"],
			evidence: { odd_field: "value-42", nested: { a: 1 } },
		});
		await renderDetail();
		expect(screen.getByText("Evidence")).toBeInTheDocument();
		expect(screen.getByText("odd field")).toBeInTheDocument();
		expect(screen.getByText("value-42")).toBeInTheDocument();
		// A nested object goes through JSON, not "[object Object]".
		expect(screen.getByText('{"a":1}')).toBeInTheDocument();
	});

	it("states there is no evidence rather than showing a blank section", async () => {
		state.alert = baseAlert({
			attackType: "Some Future Class" as RiskAlert["attackType"],
			evidence: {},
		});
		await renderDetail();
		expect(screen.getByText(/No evidence recorded/i)).toBeInTheDocument();
	});
});

describe("closing the detail keeps the table's filters", () => {
	it("the X button carries the query string back to the dashboard", async () => {
		// The alerts table keeps its filters and page in the URL and `/attacks/:id`
		// renders that table under this card. The dialog's Esc/overlay path already
		// carried the search; the visible close button did not, so the primary way
		// out of a detail view reset the operator to the default table.
		state.alert = baseAlert();
		await renderDetail(
			`/attacks/${"a".repeat(64)}?severity=HIGH&attack=Phishing&page=3`,
		);
		fireEvent.click(screen.getByTitle("Close"));
		expect(screen.getByTestId("location").textContent).toBe(
			"/dashboard?severity=HIGH&attack=Phishing&page=3",
		);
	});

	it("the not-found link carries it too", async () => {
		// Same affordance, different branch: a stale link should not silently cost
		// the operator their filters either.
		state.alert = null;
		await renderDetail(`/attacks/${"a".repeat(64)}?severity=HIGH&page=3`);
		fireEvent.click(screen.getByText("Back"));
		expect(screen.getByTestId("location").textContent).toBe(
			"/dashboard?severity=HIGH&page=3",
		);
	});
});
