import { useQuery } from "@tanstack/react-query";
import { fetchWithAuth } from "./fetch";

/**
 * Known values first, but the backend may grow new states; `string & {}`
 * keeps the union open for those without erasing the literals (a bare
 * `| string` would swallow them, and the type-checked lint flags that).
 */
type OpenEnum<Known extends string> = Known | (string & {});

export type HealthState = OpenEnum<"OK" | "DEGRADED" | "DOWN">;

export type OgmiosHealth = {
	pipeline_state: HealthState;
	chain_sync: OpenEnum<"connected" | "disconnected">;
	mempool_monitor: OpenEnum<"connected" | "disconnected">;
	circuit_breaker_chain: string;
	circuit_breaker_mempool: string;
	last_processed_slot: number;
	last_ogmios_msg_at: string;
	sync_lag_slots: number;
	sync_lag_seconds: number;
	ws_url: string;
};

export type ClusteringHealth = {
	state: OpenEnum<"ok" | "stale" | "absent" | "error">;
	// Job-heartbeat timestamp (sidecar's last feed tick), not the last published
	// anomaly: the dot tracks "clustering is running", not "an anomaly was seen".
	last_activity_at: string | null;
	age_seconds?: number;
};

export type HealthDetail = {
	status: OpenEnum<"healthy" | "degraded" | "down">;
	network: string;
	connections: number;
	pipeline_state: HealthState;
	// Absent when the ogmios client is not running: a standby (non-leader)
	// instance under the leader lock, or the startup window before the
	// client is created. Consumers must not assume it is present.
	ogmios?: OgmiosHealth;
	// Present only when the clustering sidecar module is enabled; gates the
	// Validators UI surfaces.
	clustering_enabled?: boolean;
	clustering?: ClusteringHealth;
};

async function fetchHealthDetail(): Promise<HealthDetail> {
	const res = await fetchWithAuth("/health/detail");
	if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
	return (await res.json()) as HealthDetail;
}

export function useHealth(options?: { pollMs?: number }) {
	// 30s: the TopNav is mounted on every authenticated route, so this
	// poll runs continuously. Pipeline state changes are gradual (sync
	// lag, circuit breaker state) — sub-30s freshness isn't useful and
	// just consumes rate-limit budget across all open pages.
	const pollMs = options?.pollMs ?? 30_000;
	return useQuery({
		queryKey: ["health", "detail"],
		queryFn: fetchHealthDetail,
		refetchInterval: pollMs,
		staleTime: pollMs / 2,
	});
}

/** Flatten the health payload to the module list shown in the TopNav dropdown. */
export type SystemModule = { name: string; online: boolean };

export function deriveModules(h: HealthDetail | undefined): SystemModule[] {
	if (!h) return [];
	// A standby instance (or one still starting) reports no ogmios block at
	// all; its ingestion modules genuinely aren't running there, so they
	// render offline rather than crashing the dropdown.
	const o = h.ogmios;
	const modules: SystemModule[] = [
		{ name: "Pipeline", online: h.pipeline_state === "OK" },
		{ name: "Chain Sync", online: o?.chain_sync === "connected" },
		{ name: "Mempool Monitor", online: o?.mempool_monitor === "connected" },
		{
			name: "Breakers",
			online:
				o?.circuit_breaker_chain === "CLOSED" &&
				o?.circuit_breaker_mempool === "CLOSED",
		},
	];
	// The row appears only when the sidecar module is enabled, so a plain
	// deployment isn't cluttered with a permanently-offline entry. "stale",
	// "absent" and "error" all render as offline: a sidecar that stopped
	// scoring is exactly what this row exists to make visible.
	if (h.clustering_enabled) {
		modules.push({ name: "Clustering", online: h.clustering?.state === "ok" });
	}
	return modules;
}
