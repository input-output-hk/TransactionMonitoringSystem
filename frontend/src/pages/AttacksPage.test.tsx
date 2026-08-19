/**
 * The contract-grouped risk-alerts table.
 *
 * Covers the four client-facing behaviours of the grouped view: a contract's
 * alerts collapse behind one expandable row, expanding lists that contract's
 * transactions, an alert naming no contract stays an ordinary row, and the latest
 * Critical alert is pinned first without being duplicated in the body.
 *
 * The data layer is mocked at the hook boundary (the pattern
 * NotificationsSettingsPage.test.tsx uses) rather than at `fetch`, so the test
 * exercises the page's rendering rather than the transport.
 */
import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { GroupedAlertRow } from "@/lib/api/analysis";
import type { RiskAlert } from "@/lib/attacks";
import { shortHash } from "@/lib/utils/strings";

const DJED = "addr_test1wq9djedcontractaddress0000000";
const STRIKE = "addr_test1wq9strikecontractaddress00000";

/** Mutable per-test state the mocked hooks read from. */
const state: {
	groups: GroupedAlertRow[];
	groupAlerts: RiskAlert[];
	critical: RiskAlert | null;
	avgRisk: number | null;
	/** The grouped endpoint's alert count, i.e. every alert under the filters. */
	alertTotal: number;
} = {
	groups: [],
	groupAlerts: [],
	critical: null,
	avgRisk: null,
	alertTotal: 0,
};

vi.mock("@/lib/api/analysis", async (importOriginal) => {
	const actual = await importOriginal<typeof import("@/lib/api/analysis")>();
	return {
		...actual,
		// `total` counts ROWS (what the pager steps through) and `alertTotal` counts
		// ALERTS; the page reads each for a different purpose, so the mock has to
		// keep them independent or a test could pass on the wrong one.
		useGroupedAlerts: () => ({
			data: {
				rows: state.groups,
				total: state.groups.length,
				alertTotal: state.alertTotal,
			},
			isPending: false,
			isError: false,
			error: null,
		}),
		// Two callers now: an expanded group (which always sets `contract`) and the
		// pinned critical row. `enabled` is honoured, since the page uses it to hold
		// the pinned query off entirely rather than fetching a row it will not show.
		useRiskAlerts: (
			params: {
				contract?: string;
				severities?: string[];
				pageSize?: number;
				sort?: string;
			},
			options?: { enabled?: boolean },
		) => {
			const idle = {
				data: undefined,
				isPending: false,
				isError: false,
				error: null,
			};
			if (options?.enabled === false) return idle;
			if (params.contract !== undefined) {
				return {
					data: { rows: state.groupAlerts, total: state.groupAlerts.length },
					isPending: false,
					isError: false,
					error: null,
				};
			}
			// The pinned latest-critical row.
			const rows = state.critical ? [state.critical] : [];
			return {
				data: { rows, total: rows.length },
				isPending: false,
				isError: false,
				error: null,
			};
		},
	};
});

vi.mock("@/lib/api/stats", () => ({
	useAnalysisStats: () => ({
		data: {
			total: 100,
			critical_count: 2,
			high_count: 3,
			moderate_count: 4,
			informational_count: 5,
			avg_max_score: state.avgRisk,
			finding_count: 8,
			last_analyzed_at: null,
			per_class: {},
			pending_count: 1,
		},
	}),
	useTransactionThroughput: () => ({ data: { tx_per_min: 12 } }),
	useAlertTimeseries: () => ({ data: [] }),
}));

vi.mock("@/lib/api/transactions", () => ({
	useLatestTransactions: () => ({ data: [], isPending: false }),
	useRecentBlocks: () => ({ data: [], isPending: false }),
	useTransactionDetail: () => ({
		data: null,
		isPending: false,
		isError: false,
		error: null,
	}),
}));

vi.mock("@/lib/api/health", () => ({
	useHealth: () => ({ data: { clustering_enabled: true } }),
}));

vi.mock("@/lib/api/clustering", () => ({
	useContracts: () => ({
		data: [{ target: DJED, label: "Djed StableCoin" }],
	}),
}));

function group(over: Partial<GroupedAlertRow> = {}): GroupedAlertRow {
	return {
		kind: "group",
		contractAddress: DJED,
		alertCount: 12,
		worstScore: 92,
		worstSeverity: "CRITICAL",
		latestDate: "01.08.2026, 10:00 UTC",
		...over,
	};
}

function alert(over: Partial<RiskAlert> = {}): RiskAlert {
	const hash = over.fullHash ?? `${"a".repeat(63)}1`;
	return {
		slug: hash,
		id: hash.slice(0, 8).toUpperCase(),
		fullHash: hash,
		date: "01.08.2026, 10:00 UTC",
		attackType: "Multiple Satisfaction",
		severity: "HIGH",
		riskScore: 71,
		feeAda: 0.5,
		outputs: 2,
		...over,
	};
}

/** Reports the live URL so a test can assert what navigation preserved. */
function LocationProbe() {
	const { pathname, search } = useLocation();
	return <div data-testid="location">{`${pathname}${search}`}</div>;
}

async function renderPage(entry = "/dashboard") {
	const { AttacksPage } = await import("@/pages/AttacksPage");
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<MemoryRouter initialEntries={[entry]}>
			<QueryClientProvider client={client}>
				<TooltipProvider>
					<LocationProbe />
					<AttacksPage />
				</TooltipProvider>
			</QueryClientProvider>
		</MemoryRouter>,
	);
}

beforeEach(() => {
	state.groups = [];
	state.groupAlerts = [];
	state.critical = null;
	state.avgRisk = null;
	state.alertTotal = 0;
});
afterEach(cleanup);

describe("contract grouping", () => {
	it("renders one row per contract with its count and worst severity", async () => {
		state.groups = [group()];
		await renderPage();
		// The registry label is preferred over the raw address.
		expect(screen.getByText("Djed StableCoin")).toBeInTheDocument();
		// A true server-side count, so it is safe to show as the total.
		expect(screen.getByText("12 alerts")).toBeInTheDocument();
		expect(screen.getByText("CRITICAL")).toBeInTheDocument();
	});

	it("falls back to a truncated address when the contract has no label", async () => {
		state.groups = [group({ contractAddress: STRIKE })];
		await renderPage();
		expect(screen.queryByText("Djed StableCoin")).not.toBeInTheDocument();
		expect(screen.getByTitle(STRIKE)).toBeInTheDocument();
	});

	it("singularises a one-alert group", async () => {
		state.groups = [group({ alertCount: 1 })];
		await renderPage();
		expect(screen.getByText("1 alert")).toBeInTheDocument();
	});

	it("lists the contract's transactions when expanded", async () => {
		state.groups = [group()];
		state.groupAlerts = [
			alert({ fullHash: `${"b".repeat(63)}2` }),
			alert({ fullHash: `${"c".repeat(63)}3` }),
		];
		await renderPage();
		// Collapsed: the child transactions are not rendered yet.
		expect(screen.queryByText("BBBBBBBB")).not.toBeInTheDocument();
		fireEvent.click(screen.getByText("Djed StableCoin"));
		expect(screen.getByText("BBBBBBBB")).toBeInTheDocument();
		expect(screen.getByText("CCCCCCCC")).toBeInTheDocument();
	});

	it("collapses again on a second click", async () => {
		state.groups = [group()];
		state.groupAlerts = [alert({ fullHash: `${"b".repeat(63)}2` })];
		await renderPage();
		const row = screen.getByText("Djed StableCoin");
		fireEvent.click(row);
		expect(screen.getByText("BBBBBBBB")).toBeInTheDocument();
		fireEvent.click(row);
		expect(screen.queryByText("BBBBBBBB")).not.toBeInTheDocument();
	});

	it("reports alerts beyond the expansion limit instead of hiding them", async () => {
		// A truncated list must never read as the whole group.
		state.groups = [group({ alertCount: 400 })];
		state.groupAlerts = [alert({ fullHash: `${"b".repeat(63)}2` })];
		await renderPage();
		fireEvent.click(screen.getByText("Djed StableCoin"));
		expect(
			screen.getByText(/399 older alerts are not listed/i),
		).toBeInTheDocument();
	});
});

describe("alerts with no contract", () => {
	it("renders as an ordinary row, not behind a chevron", async () => {
		state.groups = [
			{
				kind: "alert",
				contractAddress: "",
				alertCount: 1,
				worstScore: 65,
				worstSeverity: "HIGH",
				latestDate: "01.08.2026, 09:00 UTC",
				txHash: `${"d".repeat(63)}4`,
				attackType: "Phishing",
			},
		];
		await renderPage();
		// The page derives the display id through the shared truncation helper.
		expect(
			screen.getByText(shortHash(`${"d".repeat(63)}4`)),
		).toBeInTheDocument();
		expect(screen.getByText("Phishing")).toBeInTheDocument();
		// No group affordance and no collapsed count for a single alert.
		expect(screen.queryByText("1 alert")).not.toBeInTheDocument();
	});

	it("skips a malformed row that carries no transaction identity", async () => {
		state.groups = [
			{
				kind: "alert",
				contractAddress: "",
				alertCount: 1,
				worstScore: 65,
				worstSeverity: "HIGH",
				latestDate: "01.08.2026, 09:00 UTC",
			},
		];
		await renderPage();
		// Rendered as nothing rather than as a dead, unclickable row.
		expect(screen.getByText(/No alerts match/i)).toBeInTheDocument();
	});
});

describe("pinned latest critical alert", () => {
	const CRIT_HASH = `${"e".repeat(63)}5`;

	it("is the first row and is marked as pinned", async () => {
		state.critical = alert({
			fullHash: CRIT_HASH,
			severity: "CRITICAL",
			attackType: "Large Datum",
		});
		state.groups = [group()];
		await renderPage();
		expect(screen.getByText(/latest critical/i)).toBeInTheDocument();
		const rows = screen.getAllByRole("row");
		// Row 0 is the header; the pinned alert precedes the grouped rows.
		expect(rows[1].textContent).toContain("EEEEEEEE");
	});

	it("is absent when no critical alert exists", async () => {
		state.critical = null;
		state.groups = [group()];
		await renderPage();
		// No empty placeholder on a clean system.
		expect(screen.queryByText(/latest critical/i)).not.toBeInTheDocument();
	});

	it("is not duplicated when it also appears in the body", async () => {
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [
			{
				kind: "alert",
				contractAddress: "",
				alertCount: 1,
				worstScore: 92,
				worstSeverity: "CRITICAL",
				latestDate: "01.08.2026, 10:00 UTC",
				txHash: CRIT_HASH,
				attackType: "Large Datum",
			},
		];
		await renderPage();
		expect(screen.getAllByText("EEEEEEEE")).toHaveLength(1);
	});

	it("still renders when it is the only row", async () => {
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [];
		await renderPage();
		expect(screen.getByText("EEEEEEEE")).toBeInTheDocument();
		// The empty-state row must not claim there are no alerts.
		expect(screen.queryByText(/No alerts match/i)).not.toBeInTheDocument();
	});

	it("is absent when the severity filter excludes Critical", async () => {
		// The pin must never contradict the filter the operator set: a Critical
		// row above a Moderate-only table reads as the filter having failed.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group({ worstSeverity: "MODERATE" })];
		await renderPage("/dashboard?severity=MODERATE");
		expect(screen.queryByText(/latest critical/i)).not.toBeInTheDocument();
	});

	it("is present when no severity filter is applied", async () => {
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage("/dashboard?severity=");
		expect(screen.getByText(/latest critical/i)).toBeInTheDocument();
	});
});

describe("the footer alert total", () => {
	it("counts every matching alert, not just the rows on this page", async () => {
		// One group row can stand for dozens of alerts and the pager steps
		// through ROWS, so neither the row count nor this page's group counts
		// can answer "Total Risk Alerts".
		state.groups = [group({ alertCount: 12 })];
		state.alertTotal = 4213;
		await renderPage();
		expect(screen.getByText(/Total Risk Alerts: 4,213/)).toBeInTheDocument();
	});

	it("is neither the row count nor the visible group counts summed", async () => {
		// Both wrong answers are reachable from the same data, and both were
		// shipped at some point: the row count understates by orders of magnitude,
		// and summing this page's counts is a page-window artefact.
		state.groups = [group({ alertCount: 12 }), group({ alertCount: 30 })];
		state.alertTotal = 4213;
		await renderPage();
		const label = screen.getByText(/Total Risk Alerts:/);
		expect(label.textContent).toContain("4,213");
		expect(label.textContent).not.toContain("42"); // 12 + 30
		expect(label.textContent).not.toMatch(/Alerts: 2\b/); // the row count
	});
});

describe("the un-clusterable marker on a group row", () => {
	it("is shown, so the de-prioritise signal does not need an expand", async () => {
		// The backend sets this from the group's WORST alert. Without it on the row
		// the operator only learns the anomaly is structural noise after opening
		// the group, which is the opposite of what a summary row is for.
		state.groups = [group({ unclusterableModel: true })];
		await renderPage();
		expect(screen.getByText(/unclusterable model/i)).toBeInTheDocument();
	});

	it("is absent when the group's worst alert is a real finding", async () => {
		state.groups = [group({ unclusterableModel: false })];
		await renderPage();
		expect(screen.queryByText(/unclusterable model/i)).not.toBeInTheDocument();
	});
});

describe("filter state across the detail popup", () => {
	it("keeps the filters and page when a row opens the detail", async () => {
		// `/attacks/:id` renders this same page under the dialog, so dropping the
		// query string would reset the table behind the popup and leave the
		// operator at the defaults when they close it.
		const hash = `${"b".repeat(63)}2`;
		state.groups = [
			{
				kind: "alert",
				contractAddress: "",
				alertCount: 1,
				worstScore: 71,
				worstSeverity: "HIGH",
				latestDate: "01.08.2026, 10:00 UTC",
				txHash: hash,
				attackType: "Large Datum",
			},
		];
		await renderPage("/dashboard?severity=HIGH&page=2");
		fireEvent.click(screen.getByText(shortHash(hash)));
		expect(screen.getByTestId("location").textContent).toBe(
			`/attacks/${hash}?severity=HIGH&page=2`,
		);
	});
});

describe("Avg Risk KPI", () => {
	it("exposes an explanatory helper", async () => {
		state.avgRisk = 72;
		await renderPage();
		expect(screen.getByText("72.0")).toBeInTheDocument();
		expect(
			screen.getByRole("button", { name: /what avg risk means/i }),
		).toBeInTheDocument();
	});

	it("shows a placeholder when the average is not available", async () => {
		state.avgRisk = null;
		await renderPage();
		expect(
			screen.getByRole("button", { name: /what avg risk means/i }),
		).toBeInTheDocument();
	});
});

describe("the removed New Critical Attack box", () => {
	it("is gone from the KPI row", async () => {
		state.critical = alert({
			fullHash: `${"e".repeat(63)}5`,
			severity: "CRITICAL",
		});
		await renderPage();
		// The client asked for the panel to be removed; the pinned table row
		// replaces it, so this wording must not survive anywhere.
		expect(screen.queryByText(/New Critical Attack/i)).not.toBeInTheDocument();
	});
});
