import { useQuery } from "@tanstack/react-query";

export type HealthState = "OK" | "DEGRADED" | "DOWN" | string;

export type OgmiosHealth = {
	pipeline_state: HealthState;
	chain_sync: "connected" | "disconnected" | string;
	mempool_monitor: "connected" | "disconnected" | string;
	circuit_breaker_chain: string;
	circuit_breaker_mempool: string;
	last_processed_slot: number;
	last_ogmios_msg_at: string;
	sync_lag_slots: number;
	sync_lag_seconds: number;
	ws_url: string;
};

export type HealthDetail = {
	status: "healthy" | "degraded" | "down" | string;
	network: string;
	connections: number;
	pipeline_state: HealthState;
	ogmios: OgmiosHealth;
};

async function fetchHealthDetail(): Promise<HealthDetail> {
	const res = await fetch("/health/detail");
	if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
	return (await res.json()) as HealthDetail;
}

export function useHealth(options?: { pollMs?: number }) {
	const pollMs = options?.pollMs ?? 10_000;
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
	const o = h.ogmios;
	return [
		{ name: "Pipeline", online: h.pipeline_state === "OK" },
		{ name: "Chain Sync", online: o.chain_sync === "connected" },
		{ name: "Mempool Monitor", online: o.mempool_monitor === "connected" },
		{
			name: "Breakers",
			online:
				o.circuit_breaker_chain === "CLOSED" &&
				o.circuit_breaker_mempool === "CLOSED",
		},
	];
}
