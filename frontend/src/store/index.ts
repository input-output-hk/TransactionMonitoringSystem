import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { TransactionLifecycleEvent, Network } from '../types/api'

interface LiveFeedEntry {
  id: string
  event: TransactionLifecycleEvent
  ts: number
}

interface TmsStore {
  /* Settings */
  apiKey: string
  network: Network
  apiBaseUrl: string
  setApiKey: (key: string) => void
  setNetwork: (n: Network) => void
  setApiBaseUrl: (url: string) => void

  /* Live feed */
  liveEvents: LiveFeedEntry[]
  pushLiveEvent: (ev: TransactionLifecycleEvent) => void
  clearLiveEvents: () => void

  /* Webhook/alerting config */
  webhookUrl: string
  emailAlert: string
  grafanaUrl: string
  setWebhookUrl: (url: string) => void
  setEmailAlert: (email: string) => void
  setGrafanaUrl: (url: string) => void

  /* UI state */
  sidebarCollapsed: boolean
  toggleSidebar: () => void
}

export const useTmsStore = create<TmsStore>()(
  persist(
    (set) => ({
      /* Settings */
      apiKey: '',
      network: 'preprod',
      apiBaseUrl: '',
      setApiKey: (apiKey) => {
        localStorage.setItem('tms_api_key', apiKey)
        set({ apiKey })
      },
      setNetwork: (network) => set({ network }),
      setApiBaseUrl: (apiBaseUrl) => set({ apiBaseUrl }),

      /* Live feed */
      liveEvents: [],
      pushLiveEvent: (ev) =>
        set((s) => ({
          liveEvents: [
            { id: `${ev.tx_id ?? ev.status}-${Date.now()}`, event: ev, ts: Date.now() },
            ...s.liveEvents,
          ].slice(0, 100),
        })),
      clearLiveEvents: () => set({ liveEvents: [] }),

      /* Alerting */
      webhookUrl: '',
      emailAlert: '',
      grafanaUrl: '',
      setWebhookUrl: (webhookUrl) => set({ webhookUrl }),
      setEmailAlert: (emailAlert) => set({ emailAlert }),
      setGrafanaUrl: (grafanaUrl) => set({ grafanaUrl }),

      /* UI */
      sidebarCollapsed: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
    }),
    {
      name: 'tms-settings',
      partialize: (s) => ({
        apiKey: s.apiKey,
        network: s.network,
        apiBaseUrl: s.apiBaseUrl,
        webhookUrl: s.webhookUrl,
        emailAlert: s.emailAlert,
        grafanaUrl: s.grafanaUrl,
      }),
    },
  ),
)
