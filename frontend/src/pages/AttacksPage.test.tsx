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
		attackType: "Large Datum",
		attackTypes: ["Large Datum"],
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

	it("names the worst alert's attack type, and does not count in that column", async () => {
		// The Attack Type column used to hold the alert count, which promised a
		// type and delivered a number. The type shown is the worst alert's, the
		// same one the severity badge on that line describes.
		state.groups = [
			group({ attackType: "Large Datum", attackTypes: ["Large Datum"] }),
		];
		await renderPage();
		const row = screen
			.getAllByRole("row")
			.find((r) => r.textContent?.includes("Djed StableCoin"));
		expect(row?.textContent).toContain("Large Datum");
		// The count is still on the row, just no longer in that column.
		expect(screen.getByText("12 alerts")).toBeInTheDocument();
	});

	it("says how many other kinds of alert the group holds", async () => {
		// Answers "is this contract one problem or several" without an expand.
		state.groups = [
			group({
				attackType: "Large Datum",
				attackTypes: ["Large Datum", "Large Value", "Token Dust"],
			}),
		];
		await renderPage();
		const more = screen.getByText("+2 more");
		expect(more).toBeInTheDocument();
		expect(more.getAttribute("title")).toContain("Large Value");
		expect(more.getAttribute("title")).toContain("Token Dust");
	});

	it("does not offer a +N hint for a single-class group", async () => {
		state.groups = [
			group({ attackType: "Large Datum", attackTypes: ["Large Datum"] }),
		];
		await renderPage();
		expect(screen.queryByText(/\+\d+ more/)).not.toBeInTheDocument();
	});

	it("copies the FULL contract address, not the label on screen", async () => {
		// The cell shows a registry label or a truncated address, so neither can
		// be selected off the screen; the button has to hand over the real thing.
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, "clipboard", {
			value: { writeText },
			configurable: true,
		});
		state.groups = [group()];
		await renderPage();
		fireEvent.click(screen.getByLabelText("Copy contract address"));
		expect(writeText).toHaveBeenCalledWith(DJED);
	});

	it("does not expand the group when the copy button is clicked", async () => {
		// The row expands on click, so the nested control has to stop propagation.
		Object.defineProperty(navigator, "clipboard", {
			value: { writeText: vi.fn().mockResolvedValue(undefined) },
			configurable: true,
		});
		state.groups = [group()];
		state.groupAlerts = [alert({ fullHash: `${"b".repeat(63)}2` })];
		await renderPage();
		fireEvent.click(screen.getByLabelText("Copy contract address"));
		expect(screen.queryByText("BBBBBBBB")).not.toBeInTheDocument();
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
				attackTypes: ["Phishing"],
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
				// No class either: the row is skipped before it can render one.
				attackTypes: [],
			},
		];
		await renderPage();
		// Rendered as nothing rather than as a dead, unclickable row.
		expect(screen.getByText(/No alerts match/i)).toBeInTheDocument();
	});
});

describe("pinned latest critical alert", () => {
	const CRIT_HASH = `${"e".repeat(63)}5`;

	it("is the first row, ahead of the groups, and carries its own label", async () => {
		state.critical = alert({
			fullHash: CRIT_HASH,
			severity: "CRITICAL",
			attackType: "Large Datum",
		});
		state.groups = [group()];
		await renderPage();
		const rows = screen.getAllByRole("row");
		// Row 0 is the header, so the pinned alert is row 1 and the label is ON it.
		// The label used to occupy a strip of its own, which spent a table row on
		// two words and read as a second alert above the real one.
		expect(rows[1].textContent).toContain("EEEEEEEE");
		expect(rows[1].textContent).toMatch(/pinned/i);
		const groupRow = rows.findIndex((r) =>
			r.textContent?.includes("Djed StableCoin"),
		);
		expect(groupRow).toBeGreaterThan(1);
	});

	it("spends exactly one table row on the pin", async () => {
		// Guards the shape rather than the styling: an extra <tr> for the label is
		// the regression, and it is invisible to every behavioural assertion.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [];
		await renderPage();
		// Header, the pinned alert, and the spacer that separates it from the list.
		expect(screen.getAllByRole("row")).toHaveLength(3);
	});

	it("outlines the row on all four sides, with no divider crossing it", async () => {
		// Asserted on classes because the defect is purely visual: every
		// behavioural test passed while the block looked cut in half.
		//
		// An OUTLINE, not a border: preflight collapses table borders, so a `<tr>`
		// border is resolved against the cells, the row group and the column before
		// it paints, which is what made a left accent here fragile. An outline takes
		// no part in that and draws one even edge right round the row.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage();
		const row = screen.getAllByRole("row")[1];
		expect(row.className).toContain("bg-severity-critical/25");
		expect(row.className).toContain("border-b-0");
		expect(row.className).toContain("outline-1");
		expect(row.className).toContain("outline-severity-critical-foreground/60");
		// A single-sided border would leave three edges of the box undrawn.
		expect(row.className).not.toContain("border-l-2");
		expect(row.children[0].className).not.toContain("border-l-2");
	});

	it("puts the pin in the gutter, where a group row shows its chevron", async () => {
		// That column is the one an operator scans for what a row IS rather than
		// what it holds, so the pin belongs there and not beside the hash, where it
		// read as a second marker for one state.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [];
		await renderPage();
		const gutter = screen.getAllByRole("row")[1].children[0];
		expect(gutter.querySelector("svg")).not.toBeNull();
		// And the word beside the hash carries no icon of its own.
		expect(screen.getByText(/pinned/i).querySelector("svg")).toBeNull();
	});

	it("leaves no horizontal rule under the block", async () => {
		// TableRow ships `border-b`, so the spacer drew a divider 12px below the
		// tint, belonging to nothing and reading as a line out of alignment with
		// the block above it. Nothing between the pin and the list may draw one.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage();
		const rows = screen.getAllByRole("row");
		// Rows 1 and 2 are the pinned alert and the spacer that follows it.
		for (const row of [rows[1], rows[2]]) {
			expect(row.className).toContain("border-b-0");
		}
	});

	it("marks the row with plain text, not a bordered box", async () => {
		// As an outlined badge the marker was wide enough to wrap onto two lines
		// inside the ID cell: a tall red rectangle beside a single-line hash,
		// aligned with nothing. Plain text shares the hash's line box.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [];
		await renderPage();
		const marker = screen.getByText(/pinned/i);
		expect(marker.className).toContain("whitespace-nowrap");
		expect(marker.className).not.toMatch(/\bborder(-2)?\b/);
		// The severity badge on the same row already says CRITICAL, so the marker
		// must not spend width repeating it.
		expect(marker.textContent).not.toMatch(/critical/i);
	});

	it("keeps the hash on the same column as every other row's", async () => {
		// The marker trails the hash. Ahead of it, the pinned row's ID would start
		// ~80px right of every other row's and the column would look broken.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [];
		await renderPage();
		const cell = screen.getByText("EEEEEEEE").closest("td");
		const text = cell?.textContent ?? "";
		expect(text.indexOf("EEEEEEEE")).toBeLessThan(text.search(/pinned/i));
	});

	it("labels the row without explaining the sort order", async () => {
		// The sentence "kept in view regardless of the sort below" was cut as
		// superfluous: the pin icon and the position already say it. Which critical
		// alert this is stays in the marker's title, off the row.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage();
		expect(screen.getByText(/pinned/i)).toBeInTheDocument();
		expect(
			screen.queryByText(/regardless of the sort/i),
		).not.toBeInTheDocument();
	});

	it("is absent when no critical alert exists", async () => {
		state.critical = null;
		state.groups = [group()];
		await renderPage();
		// No empty placeholder on a clean system.
		expect(screen.queryByText(/pinned/i)).not.toBeInTheDocument();
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
				attackTypes: ["Large Datum"],
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

	it("names the contract it implicates, by its registry label", async () => {
		// The column is headed "Contract / ID" and every group row states its
		// contract; the pinned alert used the renderer meant for alerts that name
		// NO contract, so the most important row on the page was the one hiding the
		// attribution an analyst wants first. A second line, not a third item on the
		// first one: width is what broke the marker.
		state.critical = alert({
			fullHash: CRIT_HASH,
			severity: "CRITICAL",
			contractAddress: DJED,
		});
		state.groups = [];
		await renderPage();
		const cell = screen.getByText("EEEEEEEE").closest("td");
		expect(cell?.textContent).toContain("Djed StableCoin");
	});

	it("falls back to the truncated address when the registry has no label", async () => {
		state.critical = alert({
			fullHash: CRIT_HASH,
			severity: "CRITICAL",
			contractAddress: STRIKE,
		});
		state.groups = [];
		await renderPage();
		const cell = screen.getByText("EEEEEEEE").closest("td");
		expect(cell?.textContent).toContain(STRIKE.slice(0, 14));
		expect(cell?.textContent).not.toContain("Djed StableCoin");
	});

	it("says nothing about a contract when the alert names none", async () => {
		// An un-attributed alert is pinned as a bare hash rather than gaining an
		// empty line or a placeholder.
		state.critical = alert({
			fullHash: CRIT_HASH,
			severity: "CRITICAL",
			contractAddress: "",
		});
		state.groups = [];
		await renderPage();
		const cell = screen.getByText("EEEEEEEE").closest("td");
		// Hash, copy button and the marker, and nothing else.
		expect(cell?.textContent?.replace(/pinned/i, "").trim()).toBe("EEEEEEEE");
	});

	it("is absent past the first page, where there is no top of the list", async () => {
		// This page has no sort control: the list is date-ordered server-side, so
		// the pin's whole job is keeping the latest Critical at the top of the list.
		// Page 2 onwards has no top of the list, and the query is held off rather
		// than fetched and hidden.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage("/dashboard?page=2");
		expect(screen.queryByText(/pinned/i)).not.toBeInTheDocument();
		expect(screen.queryByText("EEEEEEEE")).not.toBeInTheDocument();
	});

	it("is present on the first page whether or not the URL says so", async () => {
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		for (const entry of ["/dashboard", "/dashboard?page=1"]) {
			await renderPage(entry);
			expect(screen.getByText(/pinned/i)).toBeInTheDocument();
			cleanup();
		}
	});

	it("is absent when the severity filter excludes Critical", async () => {
		// The pin must never contradict the filter the operator set: a Critical
		// row above a Moderate-only table reads as the filter having failed.
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group({ worstSeverity: "MODERATE" })];
		await renderPage("/dashboard?severity=MODERATE");
		expect(screen.queryByText(/pinned/i)).not.toBeInTheDocument();
	});

	it("is present when no severity filter is applied", async () => {
		state.critical = alert({ fullHash: CRIT_HASH, severity: "CRITICAL" });
		state.groups = [group()];
		await renderPage("/dashboard?severity=");
		expect(screen.getByText(/pinned/i)).toBeInTheDocument();
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

	it("survives a row that names no attack type", async () => {
		// The marker lives in the same cell as the attack type but must not depend
		// on it: "do not trust this model" is the signal that has to survive
		// longest, and a row from a backend older than attack_class still carries
		// it. Today that pairing cannot occur, which is exactly why the coupling
		// would rot unnoticed.
		state.groups = [
			group({
				unclusterableModel: true,
				attackType: undefined,
				attackTypes: [],
			}),
		];
		await renderPage();
		expect(screen.getByText(/unclusterable model/i)).toBeInTheDocument();
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
				attackTypes: ["Large Datum"],
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
