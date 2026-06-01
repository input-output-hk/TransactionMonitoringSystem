/// <reference types="vite/client" />

interface ImportMetaEnv {
	/**
	 * Opt-in to the localStorage mock shim for the archive API. Default is
	 * the real backend (`/api/archive`). Set to `"true"` in dev when you
	 * need to work offline; ignored in production builds (real client
	 * always wins so a stray flag can't silently break a deployment).
	 */
	readonly VITE_USE_MOCK_ARCHIVE_API?: string;

	/**
	 * Cardano network the frontend talks about. Used as the `network` query
	 * param on `/api/analysis/*` and `/api/archive/*`. Defaults to `preprod`
	 * (matches the backend's default).
	 */
	readonly VITE_NETWORK?: "mainnet" | "preprod" | "preview";

	/**
	 * Deployment-level API key sent as the `TMS-API-Key` header on every
	 * backend request. Leave empty in dev (backend runs in dev mode and
	 * accepts requests without an API key). REQUIRED in prod, otherwise the
	 * backend returns 403 on every authenticated route.
	 *
	 * NOTE: this is baked into the public JS bundle at build time — treat it
	 * as a deployment identifier, not a user secret.
	 */
	readonly VITE_TMS_API_KEY?: string;
}

interface ImportMeta {
	readonly env: ImportMetaEnv;
}
