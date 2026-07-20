import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchWithAuth } from "../fetch";
import {
	ClusteringApiError,
	get,
	HTTP_FORBIDDEN,
	isPermissionDenied,
	PERMISSION_DENIED_MESSAGE,
	send,
} from "./transport";

// A server error status distinct from the role gate, for the "not a permission
// problem" cases. Any non-403 !ok status exercises the same branch.
const HTTP_SERVER_ERROR = 500;

vi.mock("../fetch", () => ({
	fetchWithAuth: vi.fn(),
}));

const mockFetch = vi.mocked(fetchWithAuth);

function response(status: number, body: unknown = {}): Response {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(body),
	} as Response;
}

/** Await a rejection and hand back the thrown value as `unknown`, so the
 *  assertions narrow it explicitly instead of leaning on an implicit `any`. */
async function rejection(p: Promise<unknown>): Promise<unknown> {
	return p.then(
		() => {
			throw new Error("expected the promise to reject");
		},
		(e: unknown) => e,
	);
}

afterEach(() => {
	mockFetch.mockReset();
});

describe("clustering transport error mapping", () => {
	it("maps a 403 on a mutation to the permission-denied message", async () => {
		mockFetch.mockResolvedValue(response(HTTP_FORBIDDEN));
		const err = await rejection(send("POST", "/cluster", {}));
		expect(err).toBeInstanceOf(ClusteringApiError);
		expect((err as ClusteringApiError).status).toBe(HTTP_FORBIDDEN);
		expect((err as ClusteringApiError).message).toBe(PERMISSION_DENIED_MESSAGE);
		expect(isPermissionDenied(err)).toBe(true);
	});

	it("keeps the raw status message for a non-permission mutation failure", async () => {
		mockFetch.mockResolvedValue(response(HTTP_SERVER_ERROR));
		const err = await rejection(send("POST", "/cluster", {}));
		expect(err).toBeInstanceOf(ClusteringApiError);
		expect((err as ClusteringApiError).status).toBe(HTTP_SERVER_ERROR);
		// A 500 is a server/parameter problem, not a role rejection: it must not
		// be dressed up as a permission message.
		expect((err as ClusteringApiError).message).not.toBe(
			PERMISSION_DENIED_MESSAGE,
		);
		expect(isPermissionDenied(err)).toBe(false);
	});

	it("does not treat a 403 on a read as a permission denial", async () => {
		// Reads are open to any authenticated principal (only mutations are
		// Admin-gated), so a 403 here is a service misconfiguration, not the
		// Reviewer role gate: keep the raw status message AND report it as not a
		// permission denial, so a future read-path consumer of the guard can't
		// misclassify a sidecar key mismatch as "needs Admin".
		mockFetch.mockResolvedValue(response(HTTP_FORBIDDEN));
		const err = await rejection(get("/runs"));
		expect(err).toBeInstanceOf(ClusteringApiError);
		expect((err as ClusteringApiError).status).toBe(HTTP_FORBIDDEN);
		expect((err as ClusteringApiError).message).not.toBe(
			PERMISSION_DENIED_MESSAGE,
		);
		expect(isPermissionDenied(err)).toBe(false);
	});

	it("returns the parsed body on a successful mutation", async () => {
		mockFetch.mockResolvedValue(response(200, { job_id: "abc" }));
		await expect(send("POST", "/contracts", {})).resolves.toEqual({
			job_id: "abc",
		});
	});

	it("isPermissionDenied is false for a non-ClusteringApiError", () => {
		expect(isPermissionDenied(new Error("boom"))).toBe(false);
		expect(isPermissionDenied(null)).toBe(false);
	});
});
