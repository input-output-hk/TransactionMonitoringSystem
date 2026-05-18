import { create } from "zustand";
import { persist } from "zustand/middleware";
import { riskAlerts, type RiskAlert } from "@/mocks/attacks";

export type ArchiveMeta = {
	reason: string;
	notes?: string;
	archivedAt: string;
};

type State = {
	/** Archived alert slugs, ordered by archive time desc */
	order: string[];
	meta: Record<string, ArchiveMeta>;
};

type Actions = {
	archiveAlert: (slug: string, reason: string, notes?: string) => void;
	restoreAlert: (slug: string) => void;
};

const useArchiveStoreRaw = create<State & Actions>()(
	persist(
		(set, get) => ({
			order: [],
			meta: {},
			archiveAlert: (slug, reason, notes) => {
				const s = get();
				const archivedAt = s.meta[slug]?.archivedAt ?? new Date().toISOString();
				set({
					order: s.order.includes(slug) ? s.order : [slug, ...s.order],
					meta: { ...s.meta, [slug]: { reason, notes, archivedAt } },
				});
			},
			restoreAlert: (slug) => {
				const s = get();
				if (!s.order.includes(slug)) return;
				const nextMeta = { ...s.meta };
				delete nextMeta[slug];
				set({
					order: s.order.filter((x) => x !== slug),
					meta: nextMeta,
				});
			},
		}),
		{
			name: "tms-archive",
			partialize: (s) => ({ order: s.order, meta: s.meta }),
		},
	),
);

export const archiveAlert = (slug: string, reason: string, notes?: string) =>
	useArchiveStoreRaw.getState().archiveAlert(slug, reason, notes);

export const restoreAlert = (slug: string) =>
	useArchiveStoreRaw.getState().restoreAlert(slug);

export function isArchived(slug: string): boolean {
	return useArchiveStoreRaw.getState().order.includes(slug);
}

export function getArchiveMeta(slug: string): ArchiveMeta | undefined {
	return useArchiveStoreRaw.getState().meta[slug];
}

/** Subscribe a component to any archive change. */
export function useArchiveSnapshot() {
	return useArchiveStoreRaw((s) => s.order);
}

/** Hook: alerts not yet archived, original order. */
export function useActiveAlerts(): RiskAlert[] {
	const order = useArchiveStoreRaw((s) => s.order);
	return riskAlerts.filter((a) => !order.includes(a.slug));
}

/** Hook: archived alerts, most-recently-archived first. */
export function useArchivedAlerts(): (RiskAlert & ArchiveMeta)[] {
	const order = useArchiveStoreRaw((s) => s.order);
	const meta = useArchiveStoreRaw((s) => s.meta);
	const bySlug = new Map(riskAlerts.map((a) => [a.slug, a]));
	return order
		.map((slug) => {
			const a = bySlug.get(slug);
			const m = meta[slug];
			return a && m ? { ...a, ...m } : null;
		})
		.filter((x): x is RiskAlert & ArchiveMeta => x !== null);
}
