import type { RiskBand } from '../../types/api'
import styles from './RiskBadge.module.css'

interface Props {
  band: RiskBand
  score?: number
  size?: 'sm' | 'md' | 'lg'
}

export const RISK_COLORS: Record<RiskBand, string> = {
  Critical: 'var(--risk-critical)',
  High:     'var(--risk-high)',
  Moderate: 'var(--risk-moderate)',
  Low:      'var(--risk-low)',
}

export const RISK_BG: Record<RiskBand, string> = {
  Critical: 'var(--risk-critical-bg)',
  High:     'var(--risk-high-bg)',
  Moderate: 'var(--risk-moderate-bg)',
  Low:      'var(--risk-low-bg)',
}

export default function RiskBadge({ band, score, size = 'md' }: Props) {
  return (
    <span
      className={`${styles.badge} ${styles[size]}`}
      style={{
        '--badge-color': RISK_COLORS[band],
        '--badge-bg': RISK_BG[band],
      } as React.CSSProperties}
    >
      <span className={styles.dot} />
      {band}
      {score !== undefined && <span className={styles.score}>{score.toFixed(1)}</span>}
    </span>
  )
}
