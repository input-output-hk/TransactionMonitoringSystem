/**
 * The transactions inside one expanded contract group.
 *
 * Fetched lazily, only while the group is open, using the same filters the
 * grouped row was counted under so the rows shown match its `alertCount`. The
 * query is keyed by contract, so collapsing and re-expanding is served from
 * cache rather than re-fetching.
 */
import { Fragment } from "react";

import { AlertRow, GroupMessageRow } from "@/components/alerts/alert-rows";
import { useRiskAlerts, type RiskAlertsParams } from "@/lib/api/analysis";

/**
 * Rows fetched per expanded group. A group's own count is authoritative and is
 * displayed on the summary row; this only bounds one expansion's request. When a
 * group holds more than this, the surplus is stated rather than silently
 * dropped: an operator must never read a truncated list as the whole group.
 */
export const GROUP_EXPANSION_LIMIT = 50;

export function ContractGroupAlerts({
	contract,
	alertCount,
	filters,
	onOpen,
}: {
	contract: string;
	/** Server-side total for the group, used to report anything beyond the limit. */
	alertCount: number;
	/** The table's active filters, so expansion agrees with the group's count. */
	filters: Omit<RiskAlertsParams, "page" | "pageSize" | "contract">;
	onOpen: (slug: string) => void;
}) {
	const { data, isPending, isError, error } = useRiskAlerts({
		...filters,
		page: 0,
		pageSize: GROUP_EXPANSION_LIMIT,
		contract,
	});

	if (isPending) {
		return <GroupMessageRow>Loading this contract's alerts…</GroupMessageRow>;
	}
	if (isError) {
		return (
			<GroupMessageRow>
				{`Failed to load: ${error instanceof Error ? error.message : "unknown error"}`}
			</GroupMessageRow>
		);
	}
	const rows = data?.rows ?? [];
	if (rows.length === 0) {
		return <GroupMessageRow>No alerts for this contract.</GroupMessageRow>;
	}
	const hidden = alertCount - rows.length;
	return (
		<Fragment>
			{rows.map((alert) => (
				<AlertRow key={alert.slug} alert={alert} onOpen={onOpen} indented />
			))}
			{hidden > 0 && (
				<GroupMessageRow>
					{`Showing the ${rows.length} most recent of ${alertCount}. ${hidden} older ${
						hidden === 1 ? "alert is" : "alerts are"
					} not listed here.`}
				</GroupMessageRow>
			)}
		</Fragment>
	);
}
