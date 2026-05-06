import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, X } from 'lucide-react'
import { fetchAlerts, fetchScoreResult } from '../lib/api'
import { ATTACK_CLASSES, ATTACK_CLASS_LABELS, RISK_BAND_ORDER, type AttackClass, type RiskBand, type ClassScoreResult } from '../types/api'
import RiskBadge from '../components/ui/RiskBadge'
import TxHash from '../components/ui/TxHash'
import AttackClassBadge from '../components/ui/AttackClassBadge'
import ScoreBar from '../components/ui/ScoreBar'
import styles from './Alerts.module.css'

type SortKey = 'score' | 'date'

function timeAgo(iso: string): string {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export default function Alerts() {
  const [band, setBand] = useState<RiskBand | 'All'>('All')
  const [cls, setCls] = useState<AttackClass | 'all'>('all')
  const [sort, setSort] = useState<SortKey>('score')
  const [selected, setSelected] = useState<ClassScoreResult | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['alerts', band, cls, sort],
    queryFn: () => fetchAlerts({ band, attack_class: cls, limit: 50, sort }),
    refetchInterval: 30_000,
  })

  const { data: detail } = useQuery({
    queryKey: ['score-detail', selected?.tx_hash],
    queryFn: () => fetchScoreResult(selected!.tx_hash),
    enabled: !!selected,
  })

  const alerts = data?.items ?? []

  return (
    <div className={styles.root}>
      {/* Filters */}
      <div className={styles.filters}>
        <div className={styles.filterGroup}>
          <span className={styles.filterLabel}>Band</span>
          {(['All', ...RISK_BAND_ORDER] as const).map((b) => (
            <button
              key={b}
              className={`${styles.filterBtn} ${band === b ? styles.filterBtnActive : ''}`}
              onClick={() => setBand(b)}
              data-band={b}
            >
              {b}
            </button>
          ))}
        </div>

        <div className={styles.filterGroup}>
          <span className={styles.filterLabel}>Sort</span>
          <button
            className={`${styles.filterBtn} ${sort === 'score' ? styles.filterBtnActive : ''}`}
            onClick={() => setSort('score')}
          >
            By Score
          </button>
          <button
            className={`${styles.filterBtn} ${sort === 'date' ? styles.filterBtnActive : ''}`}
            onClick={() => setSort('date')}
          >
            By Date
          </button>
        </div>

        <div className={styles.classPills}>
          <button
            className={`${styles.classPill} ${cls === 'all' ? styles.classPillActive : ''}`}
            onClick={() => setCls('all')}
          >
            All classes
          </button>
          {ATTACK_CLASSES.map((c) => (
            <button
              key={c}
              className={`${styles.classPill} ${cls === c ? styles.classPillActive : ''}`}
              onClick={() => setCls(c === cls ? 'all' : c)}
            >
              {ATTACK_CLASS_LABELS[c]}
            </button>
          ))}
        </div>
      </div>

      {/* Table + detail panel */}
      <div className={styles.body} data-has-detail={!!selected}>
        {/* Table */}
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Tx Hash</th>
                <th>Time</th>
                <th>Attack Class</th>
                <th>Score <SortIcon active={sort === 'score'} /></th>
                <th>Band</th>
              </tr>
            </thead>
            <tbody>
              {isLoading && Array.from({ length: 8 }).map((_, i) => (
                <tr key={i} className={styles.skeletonRow}>
                  <td><div className="skeleton" style={{ width: 110, height: 14 }} /></td>
                  <td><div className="skeleton" style={{ width: 60, height: 12 }} /></td>
                  <td><div className="skeleton" style={{ width: 80, height: 20 }} /></td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <div className="skeleton" style={{ flex: 1, height: 5 }} />
                      <div className="skeleton" style={{ width: 28, height: 12 }} />
                    </div>
                  </td>
                  <td><div className="skeleton" style={{ width: 70, height: 20 }} /></td>
                </tr>
              ))}
              {!isLoading && alerts.length === 0 && (
                <tr>
                  <td colSpan={5} className={styles.empty}>No alerts match the selected filters</td>
                </tr>
              )}
              {alerts.filter((a) => a.tx_hash).map((alert) => (
                <tr
                  key={alert.tx_hash}
                  className={`${styles.row} ${selected?.tx_hash === alert.tx_hash ? styles.rowSelected : ''}`}
                  onClick={() => setSelected(selected?.tx_hash === alert.tx_hash ? null : alert)}
                >
                  <td><TxHash hash={alert.tx_hash} link chars={10} /></td>
                  <td className={styles.timeCell}>{timeAgo(alert.analyzed_at)}</td>
                  <td><AttackClassBadge cls={alert.max_class} size="sm" /></td>
                  <td>
                    <div className={styles.scoreCell}>
                      <ScoreBar score={alert.max_score} band={alert.risk_band} height={4} />
                      <span className={styles.scoreNum}>{alert.max_score.toFixed(1)}</span>
                    </div>
                  </td>
                  <td><RiskBadge band={alert.risk_band} size="sm" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Detail panel */}
        {selected && (
          <div className={styles.detailPanel}>
            <div className={styles.detailHeader}>
              <h3 className={styles.detailTitle}>Score Breakdown</h3>
              <button className={styles.closeBtn} onClick={() => setSelected(null)}>
                <X size={14} />
              </button>
            </div>

            <div className={styles.detailMeta}>
              <TxHash hash={selected.tx_hash} link />
              <RiskBadge band={selected.risk_band} score={selected.max_score} size="md" />
            </div>

            <div className={styles.scoreGrid}>
              {ATTACK_CLASSES.map((c) => {
                const s = detail?.scores[c] ?? selected.scores[c]
                if (s === -1) return null
                return (
                  <div key={c} className={styles.scoreRow}>
                    <span className={styles.scoreLabel}>
                      <AttackClassBadge cls={c} size="sm" />
                    </span>
                    <ScoreBar score={s} height={5} showLabel />
                  </div>
                )
              })}
            </div>

            {/* Sub-score detail for dominant class */}
            <div className={styles.subSection}>
              <h4 className={styles.subTitle}>
                Top signals — <AttackClassBadge cls={selected.max_class} size="sm" />
              </h4>
              {Object.entries(detail?.sub_scores[selected.max_class] ?? selected.sub_scores[selected.max_class] ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([key, val]) => (
                  <div key={key} className={styles.subRow}>
                    <span className={styles.subKey}>{key.replace(/_/g, ' ')}</span>
                    <div className={styles.subBar}>
                      <div
                        className={styles.subFill}
                        style={{ width: `${val * 100}%`, opacity: 0.7 + val * 0.3 }}
                      />
                    </div>
                    <span className={styles.subVal}>{(val * 100).toFixed(0)}%</span>
                  </div>
                ))
              }
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function SortIcon({ active }: { active: boolean }) {
  return active
    ? <ChevronDown size={11} style={{ display: 'inline', verticalAlign: 'middle' }} />
    : <ChevronUp size={11} style={{ display: 'inline', verticalAlign: 'middle', opacity: 0.3 }} />
}
