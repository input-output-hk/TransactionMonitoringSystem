/// <reference types="vite/client" />

interface ImportMetaEnv {
	/**
	 * When `"true"`, the archive API client talks to the real backend at
	 * `/api/archive`. Otherwise (default in dev) a localStorage-backed mock
	 * shim is used so the UI works in isolation while the backend is built.
	 * Always treated as `true` in production builds regardless of value.
	 */
	readonly VITE_USE_REAL_ARCHIVE_API?: string;
}

interface ImportMeta {
	readonly env: ImportMetaEnv;
}
