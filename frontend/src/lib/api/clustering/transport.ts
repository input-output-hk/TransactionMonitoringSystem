// Same-origin transport for the clustering reverse-proxy. Internal to the
// client (the barrel does not re-export these); hooks call get/send.
import { fetchWithAuth } from "../fetch";
import type { Validator } from "./validation";

export const BASE = "/api/clustering";

export async function get<T>(
	path: string,
	validate?: Validator<T>,
): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`);
	if (!res.ok) throw new Error(`clustering ${path} failed: ${res.status}`);
	const raw = await res.json();
	return validate ? validate(raw) : (raw as T);
}

export async function send<T>(
	method: string,
	path: string,
	body?: unknown,
	validate?: Validator<T>,
): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`, {
		method,
		headers: { "Content-Type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	if (!res.ok)
		throw new Error(`clustering ${method} ${path} failed: ${res.status}`);
	const raw = await res.json();
	return validate ? validate(raw) : (raw as T);
}
