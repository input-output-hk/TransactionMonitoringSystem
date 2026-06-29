// React Query hooks: deployment config. Public surface (re-exported by the
// barrel) together with the types.
import { useQuery } from "@tanstack/react-query";

import type { ClusteringConfig } from "../types";
import { validateConfig } from "../validation";
import { get } from "../transport";

/** Read-only deployment config. Static for a deployment's lifetime, so it never
 *  refetches; the onboarding form reads `host_backed` to decide whether the
 *  per-contract "max txs" control applies. */
export function useClusteringConfig(enabled = true) {
	return useQuery({
		queryKey: ["clustering", "config"],
		queryFn: () => get<ClusteringConfig>("/config", validateConfig),
		enabled,
		staleTime: Infinity,
	});
}
