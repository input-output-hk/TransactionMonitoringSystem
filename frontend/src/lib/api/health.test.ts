import { describe, expect, it } from "vitest";
import { deriveModules, type HealthDetail } from "./health";

function healthFixture(overrides: Partial<HealthDetail> = {}): HealthDetail {
	return {
		status: "healthy",
		network: "preprod",
		connections: 1,
		pipeline_state: "OK",
		ogmios: {
			pipeline_state: "OK",
			chain_sync: "connected",
			mempool_monitor: "connected",
			circuit_breaker_chain: "CLOSED",
			circuit_breaker_mempool: "CLOSED",
			last_processed_slot: 100,
			last_ogmios_msg_at: "2026-01-01T00:00:00Z",
			sync_lag_slots: 0,
			sync_lag_seconds: 0,
			ws_url: "ws://localhost:1337",
		},
		...overrides,
	};
}

describe("deriveModules", () => {
	it("returns no modules while health is still loading", () => {
		expect(deriveModules(undefined)).toEqual([]);
	});

	it("omits the Clustering row when the sidecar module is disabled", () => {
		const names = deriveModules(healthFixture()).map((m) => m.name);
		expect(names).toEqual([
			"Pipeline",
			"Chain Sync",
			"Mempool Monitor",
			"Breakers",
		]);
	});

	it("shows Clustering online when the sidecar is enabled and ok", () => {
		const modules = deriveModules(
			healthFixture({
				clustering_enabled: true,
				clustering: { state: "ok", last_scored_at: "2026-01-01T00:00:00Z" },
			}),
		);
		expect(modules).toContainEqual({ name: "Clustering", online: true });
	});

	it.each(["stale", "absent", "error"])(
		"shows Clustering offline when the sidecar reports %s",
		(state) => {
			const modules = deriveModules(
				healthFixture({
					clustering_enabled: true,
					clustering: { state, last_scored_at: null },
				}),
			);
			expect(modules).toContainEqual({ name: "Clustering", online: false });
		},
	);

	it("shows Clustering offline when enabled but the payload is missing", () => {
		// clustering_enabled without a clustering block means the host could
		// not reach the sidecar at all; that must read as offline, not as
		// "row silently missing".
		const modules = deriveModules(healthFixture({ clustering_enabled: true }));
		expect(modules).toContainEqual({ name: "Clustering", online: false });
	});
});
