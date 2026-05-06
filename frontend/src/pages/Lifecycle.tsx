import { useQuery } from '@tanstack/react-query'
import { fetchLifecycleStats, fetchLifecycleEvents } from '../lib/api'
import { useTmsStore } from '../store'
import TxHash from '../components/ui/TxHash'
import styles from './Lifecycle.module.css'

function ms(n: number | null): string {
  if (n === null) return '—'
  if (n < 1000) return `${n}ms`
  return `${(n / 1000).toFixed(1)}s`
}

function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const STATUS_COLORS: Record<string, string> = {
  PENDING:      'var(--risk-moderate)',
  CONFIRMED:    'var(--risk-low)',
  ROLLED_BACK:  'var(--risk-critical)',
  DROPPED:      'var(--risk-none)',
}

export default function Lifecycle() {
  const liveEvents = useTmsStore((s) => s.liveEvents)

  const { data: stats } = useQuery({
    queryKey: ['lifecycleStats'],
    queryFn: fetchLifecycleStats,
    refetchInterval: 10_000,
  })

  const { data: pending } = useQuery({
    queryKey: ['lifecycle-pending'],
    queryFn: () => fetchLifecycleEvents({ status: 'PENDING', limit: 20 }),
    refetchInterval: 15_000,
  })

  const s = stats ?? {
    pending: 847, confirmed: 141_983, rolled_back: 312,
    dropped: 1_204, avg_latency_ms: 18_420, rollback_rate: 0.0022,
  }

  return (
    <div className={styles.root}>
      {/* Stat cards */}
      <div className={styles.statGrid}>
        {[
          { label: 'Pending', value: s.pending, color: 'var(--risk-moderate)' },
          { label: 'Confirmed', value: s.confirmed, color: 'var(--risk-low)' },
          { label: 'Rolled Back', value: s.rolled_back, color: 'var(--risk-critical)' },
          { label: 'Dropped', value: s.dropped, color: 'var(--risk-none)' },
        ].map(({ label, value, color }) => (
          <div key={label} className={styles.statCard} style={{ '--stat-color': color } as React.CSSProperties}>
            <span className={styles.statLabel}>{label}</span>
            <span className={styles.statValue}>{value.toLocaleString()}</span>
          </div>
        ))}

        <div className={styles.statCard} style={{ '--stat-color': 'var(--accent)' } as React.CSSProperties}>
          <span className={styles.statLabel}>Avg Confirmation</span>
          <span className={styles.statValue}>{ms(s.avg_latency_ms)}</span>
        </div>

        <div className={styles.statCard} style={{ '--stat-color': 'var(--risk-high)' } as React.CSSProperties}>
          <span className={styles.statLabel}>Rollback Rate</span>
          <span className={styles.statValue}>{(s.rollback_rate * 100).toFixed(3)}%</span>
        </div>
      </div>

      {/* State machine diagram */}
      <div className={styles.card}>
        <h3 className={styles.cardTitle}>Transaction Lifecycle States</h3>
        <div className={styles.stateMachine}>
          {[
            { state: 'PENDING', desc: 'Seen in mempool', next: ['CONFIRMED', 'DROPPED'] },
            { state: 'CONFIRMED', desc: 'Included in block', next: ['ROLLED_BACK'] },
            { state: 'ROLLED_BACK', desc: 'Block reorganized', next: ['PENDING'] },
            { state: 'DROPPED', desc: 'Expired from mempool', next: [] },
          ].map(({ state, desc, next }) => (
            <div key={state} className={styles.stateNode}>
              <div
                className={styles.stateBox}
                style={{ '--state-color': STATUS_COLORS[state] } as React.CSSProperties}
              >
                <span className={styles.stateName}>{state}</span>
                <span className={styles.stateDesc}>{desc}</span>
              </div>
              {next.length > 0 && (
                <div className={styles.stateArrows}>
                  {next.map((n) => (
                    <span key={n} className={styles.stateArrow}>→ {n}</span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className={styles.panels}>
        {/* Pending txs */}
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Pending Transactions</h3>
          <div className={styles.eventList}>
            {(pending?.items ?? []).length === 0 && (
              <div className={styles.empty}>No pending transactions</div>
            )}
            {(pending?.items ?? []).map((ev) => (
              <div key={ev.tx_id} className={styles.eventRow}>
                <TxHash hash={ev.tx_id} link />
                <span className={styles.eventTime}>{timeAgo(ev.first_seen_at)}</span>
                <span className={styles.eventStatus} style={{ color: STATUS_COLORS[ev.status] }}>
                  {ev.status}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Live WebSocket feed */}
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Live Events (WebSocket)</h3>
          <div className={styles.eventList}>
            {liveEvents.length === 0 && (
              <div className={styles.empty}>Waiting for live events…</div>
            )}
            {liveEvents.slice(0, 30).map((entry) => (
              <div key={entry.id} className={`${styles.eventRow} animate-fade-in`}>
                <TxHash hash={entry.event.tx_id} link />
                <span className={styles.eventTime}>{timeAgo(entry.event.confirmed_at ?? entry.event.first_seen_at)}</span>
                <span className={styles.eventStatus} style={{ color: STATUS_COLORS[entry.event.status] }}>
                  {entry.event.status}
                </span>
                {entry.event.height && (
                  <span className={styles.eventBlock}>#{entry.event.height.toLocaleString()}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
