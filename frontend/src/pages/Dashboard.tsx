import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from 'recharts'
import { AlertTriangle, ArrowLeftRight, Activity, Cpu } from 'lucide-react'
import { fetchTransactionStats, fetchAnalysisStats, fetchLifecycleStats, fetchAlerts, fetchHealth } from '../lib/api'
import { ATTACK_CLASS_LABELS, type AttackClass } from '../types/api'
import KPICard from '../components/ui/KPICard'
import RiskBadge from '../components/ui/RiskBadge'
import TxHash from '../components/ui/TxHash'
import AttackClassBadge from '../components/ui/AttackClassBadge'
import ScoreBar from '../components/ui/ScoreBar'
import LiveDot from '../components/ui/LiveDot'
import { useTmsStore } from '../store'
import styles from './Dashboard.module.css'

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000)    return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function ada(lovelace: number): string {
  return `${(lovelace / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 0 })} ₳`
}

const CLASS_COLORS: Record<string, string> = {
  token_dust:    '#6366F1',
  large_value:   '#EC4899',
  large_datum:   '#8B5CF6',
  multiple_sat:  '#F59E0B',
  front_running: '#EF4444',
  sandwich:      '#F97316',
  circular:      '#06B6D4',
  fake_token:    '#84CC16',
  phishing:      '#E879F9',
}

export default function Dashboard() {
  const liveEvents = useTmsStore((s) => s.liveEvents)

  const { data: txStats } = useQuery({
    queryKey: ['txStats'],
    queryFn: fetchTransactionStats,
    refetchInterval: 30_000,
  })

  const { data: analysisStats } = useQuery({
    queryKey: ['analysisStats'],
    queryFn: fetchAnalysisStats,
    refetchInterval: 30_000,
  })

  const { data: lcStats } = useQuery({
    queryKey: ['lifecycleStats'],
    queryFn: fetchLifecycleStats,
    refetchInterval: 15_000,
  })

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  })

  const { data: alertsData } = useQuery({
    queryKey: ['alerts-dashboard'],
    queryFn: () => fetchAlerts({ band: 'Critical', limit: 8, sort: 'score' }),
    refetchInterval: 20_000,
  })

  const bandCounts = analysisStats?.band_counts
  const totalAlerts = bandCounts
    ? (bandCounts.Critical ?? 0) + (bandCounts.High ?? 0) + (bandCounts.Moderate ?? 0)
    : 0
  const criticalCount = bandCounts?.Critical ?? 0

  const classChartData = analysisStats
    ? Object.entries(analysisStats.class_distributions).map(([cls, d]) => ({
        name: ATTACK_CLASS_LABELS[cls as AttackClass],
        cls,
        mean: Math.round(d.mean * 10) / 10,
        p95: Math.round(d.p95 * 10) / 10,
        count: d.count,
      }))
    : []

  return (
    <div className={styles.root}>
      {/* KPI strip */}
      <div className={styles.kpiGrid}>
        <KPICard
          label="Transactions (24h)"
          value={fmt(txStats?.count ?? 142_830)}
          sub={`${ada(txStats?.total_volume ?? 4_812_000_000_000)} volume`}
          accent="var(--accent)"
          icon={<ArrowLeftRight size={15} />}
        />
        <KPICard
          label="Critical Alerts"
          value={<span style={{ color: 'var(--risk-critical)' }}>{criticalCount}</span>}
          sub={`${totalAlerts} total flagged transactions`}
          accent="var(--risk-critical)"
          icon={<AlertTriangle size={15} />}
          pulse={criticalCount > 0}
        />
        <KPICard
          label="Pending / Confirmed"
          value={
            <span>
              <span style={{ color: 'var(--risk-moderate)' }}>{fmt(lcStats?.pending ?? 847)}</span>
              <span style={{ color: 'var(--text-muted)', fontSize: 18, margin: '0 6px' }}>/</span>
              <span style={{ color: 'var(--risk-low)' }}>{fmt(lcStats?.confirmed ?? 141_983)}</span>
            </span>
          }
          sub={`Rollback rate: ${((lcStats?.rollback_rate ?? 0.0022) * 100).toFixed(2)}%`}
          accent="var(--risk-moderate)"
          icon={<Activity size={15} />}
        />
        <KPICard
          label="System Status"
          value={
            <span style={{
              color: health?.pipeline_state === 'OK' ? 'var(--risk-low)' : 'var(--risk-high)',
              fontSize: 18,
              fontWeight: 600,
            }}>
              {health?.status === 'demo' ? 'Demo Mode' : health?.pipeline_state === 'OK' ? 'Online' : 'Degraded'}
            </span>
          }
          sub={health?.ogmios_connected ? 'Ogmios connected' : 'Ogmios offline'}
          accent={health?.pipeline_state === 'OK' ? 'var(--risk-low)' : 'var(--risk-high)'}
          icon={<Cpu size={15} />}
        />
      </div>

      {/* Main grid */}
      <div className={styles.mainGrid}>
        {/* Left: Recent critical alerts */}
        <section className={styles.alertPanel}>
          <div className={styles.panelHeader}>
            <h2 className={styles.panelTitle}>Critical Alerts</h2>
            <a href="/alerts" className={styles.viewAll}>View all →</a>
          </div>

          <div className={styles.alertTable}>
            <div className={styles.alertHead}>
              <span>Tx Hash</span>
              <span>Class</span>
              <span>Score</span>
              <span>Band</span>
            </div>
            {(alertsData?.items ?? []).filter((a) => a.tx_hash).map((alert) => (
              <div key={alert.tx_hash} className={styles.alertRow}>
                <TxHash hash={alert.tx_hash} link />
                <AttackClassBadge cls={alert.max_class} size="sm" />
                <div className={styles.scoreCell}>
                  <ScoreBar score={alert.max_score} band={alert.risk_band} height={3} />
                  <span className={styles.scoreNum}>{alert.max_score.toFixed(0)}</span>
                </div>
                <RiskBadge band={alert.risk_band} size="sm" />
              </div>
            ))}
            {!alertsData && (
              Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className={styles.alertRowSkeleton}>
                  <div className="skeleton" style={{ width: 100, height: 14 }} />
                  <div className="skeleton" style={{ width: 70, height: 14 }} />
                  <div className="skeleton" style={{ width: 80, height: 6 }} />
                  <div className="skeleton" style={{ width: 60, height: 14 }} />
                </div>
              ))
            )}
          </div>
        </section>

        {/* Right: Live feed */}
        <section className={styles.feedPanel}>
          <div className={styles.panelHeader}>
            <h2 className={styles.panelTitle}>Live Feed</h2>
            <span className={styles.liveTag}>
              <LiveDot status={liveEvents.length > 0 ? 'connected' : 'disconnected'} size={6} />
              WebSocket
            </span>
          </div>

          <div className={styles.feedList}>
            {liveEvents.length === 0 && (
              <div className={styles.emptyFeed}>
                <Activity size={24} color="var(--text-muted)" />
                <span>Waiting for live events…</span>
                <span className={styles.emptyFeedSub}>Connect to a Cardano node via Ogmios to stream real-time transactions</span>
              </div>
            )}
            {liveEvents.map((entry) => (
              <div key={entry.id} className={`${styles.feedItem} animate-fade-in`}>
                <span
                  className={styles.feedStatus}
                  style={{
                    color: entry.event.status === 'CONFIRMED' ? 'var(--risk-low)'
                      : entry.event.status === 'ROLLED_BACK' ? 'var(--risk-critical)'
                      : entry.event.status === 'PENDING' ? 'var(--risk-moderate)'
                      : 'var(--text-muted)',
                  }}
                >
                  {entry.event.status}
                </span>
                <TxHash hash={entry.event.tx_id} link />
                {entry.event.height && (
                  <span className={styles.feedBlock}>#{entry.event.height.toLocaleString()}</span>
                )}
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* Score distribution by class */}
      <section className={styles.chartPanel}>
        <div className={styles.panelHeader}>
          <h2 className={styles.panelTitle}>Score Distribution by Attack Class</h2>
          <span className={styles.chartSub}>Mean and P95 scores across {fmt(analysisStats?.total_analyzed ?? 2630)} analyzed transactions</span>
        </div>

        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={classChartData} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
            <XAxis
              dataKey="name"
              tick={{ fill: 'var(--text-secondary)', fontSize: 10 }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              domain={[0, 100]}
              tick={{ fill: 'var(--text-muted)', fontSize: 10 }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border-default)',
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: 'var(--text-primary)', fontWeight: 600 }}
              itemStyle={{ color: 'var(--text-secondary)' }}
              formatter={(val: number, name: string) => [
                val.toFixed(1),
                name === 'p95' ? 'P95 Score' : 'Mean Score',
              ]}
            />
            <Bar dataKey="mean" radius={[3, 3, 0, 0]} maxBarSize={20}>
              {classChartData.map((entry) => (
                <Cell key={entry.cls} fill={CLASS_COLORS[entry.cls] ?? '#6366F1'} fillOpacity={0.7} />
              ))}
            </Bar>
            <Bar dataKey="p95" radius={[3, 3, 0, 0]} maxBarSize={20}>
              {classChartData.map((entry) => (
                <Cell key={entry.cls} fill={CLASS_COLORS[entry.cls] ?? '#6366F1'} fillOpacity={0.35} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>

        {/* Risk band summary */}
        <div className={styles.bandStrip}>
          {(['Critical', 'High', 'Moderate', 'Low'] as const).map((band) => (
            <div key={band} className={styles.bandItem}>
              <RiskBadge band={band} size="sm" />
              <span className={styles.bandCount}>{bandCounts?.[band] ?? 0}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
