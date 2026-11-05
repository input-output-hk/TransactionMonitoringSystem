import { describe, expect, it } from "vitest";
import { deriveModules, type HealthDetail, type ScoringHealth } from "./health";

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
				clustering: { state: "ok", last_activity_at: "2026-01-01T00:00:00Z" },
			}),
		);
		expect(modules).toContainEqual({ name: "Clustering", status: "online" });
	});

	it.each(["stale", "absent", "error"])(
		"shows Clustering offline when the sidecar reports %s",
		(state) => {
			const modules = deriveModules(
				healthFixture({
					clustering_enabled: true,
					clustering: { state, last_activity_at: null },
				}),
			);
			expect(modules).toContainEqual({ name: "Clustering", status: "offline" });
		},
	);

	it("survives a payload without ogmios (standby instance) instead of crashing", () => {
		// A non-leader standby never starts the ogmios client, so the backend
		// omits the whole block; the ingestion modules render offline there.
		const noOgmios = { ...healthFixture(), ogmios: undefined };
		const modules = deriveModules(noOgmios);
		expect(modules.map((m) => m.name)).toEqual([
			"Pipeline",
			"Chain Sync",
			"Mempool Monitor",
			"Breakers",
		]);
		expect(modules.find((m) => m.name === "Chain Sync")?.status).toBe(
			"offline",
		);
		expect(modules.find((m) => m.name === "Breakers")?.status).toBe("offline");
	});

	it("shows Clustering offline when enabled but the payload is missing", () => {
		// clustering_enabled without a clustering block means the host could
		// not reach the sidecar at all; that must read as offline, not as
		// "row silently missing".
		const modules = deriveModules(healthFixture({ clustering_enabled: true }));
		expect(modules).toContainEqual({ name: "Clustering", status: "offline" });
	});

	function scoringFixture(state: string): ScoringHealth {
		return {
			state,
			live_window_seconds: 600,
			live_polled_at: "2026-11-02T12:00:00Z",
			backlog_enabled: true,
			checkpoint: "2026-11-02T10:50:00Z",
			oldest_unscored_at: null,
		};
	}

	it("shows Scoring online when the lanes are caught up", () => {
		const modules = deriveModules(
			healthFixture({ scoring: scoringFixture("ok") }),
		);
		expect(modules).toContainEqual({ name: "Scoring", status: "online" });
	});

	it.each(["behind", "starting", "unguarded"])(
		"shows Scoring amber, not red, when %s: live transactions are still scored",
		(state) => {
			// Ingestion at the tip says nothing about scoring: this is the row
			// that turns the overall dot amber when transactions go unscored.
			const modules = deriveModules(
				healthFixture({ scoring: scoringFixture(state) }),
			);
			expect(modules).toContainEqual({ name: "Scoring", status: "warning" });
			expect(modules.find((m) => m.name === "Chain Sync")?.status).toBe(
				"online",
			);
		},
	);

	it.each(["stalled", "disabled", "a-state-this-build-does-not-know"])(
		"shows Scoring red when %s: nothing is being scored",
		(state) => {
			const modules = deriveModules(
				healthFixture({ scoring: scoringFixture(state) }),
			);
			expect(modules).toContainEqual({ name: "Scoring", status: "offline" });
		},
	);

	it("shows Scoring red when unscored rows are about to expire, even while behind", () => {
		const scoring = { ...scoringFixture("behind"), retention_at_risk: true };
		const modules = deriveModules(healthFixture({ scoring }));
		expect(modules).toContainEqual({ name: "Scoring", status: "offline" });
	});

	it("omits the Scoring row on an instance that does not score", () => {
		const names = deriveModules(healthFixture()).map((m) => m.name);
		expect(names).not.toContain("Scoring");
	});
});
