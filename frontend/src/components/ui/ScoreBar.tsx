import styles from './ScoreBar.module.css'
import { RISK_COLORS } from './RiskBadge'
import type { RiskBand } from '../../types/api'

interface Props {
  score: number
  band?: RiskBand
  showLabel?: boolean
  height?: number
}

function scoreToColor(score: number, band?: RiskBand): string {
  if (band) return RISK_COLORS[band]
  if (score >= 80) return RISK_COLORS.Critical
  if (score >= 60) return RISK_COLORS.High
  if (score >= 31) return RISK_COLORS.Moderate
  return RISK_COLORS.Low
}

export default function ScoreBar({ score, band, showLabel = false, height = 4 }: Props) {
  const color = scoreToColor(score, band)
  const pct = Math.min(100, Math.max(0, score))

  return (
    <div className={styles.wrap}>
      <div
        className={styles.track}
        role="progressbar"
        aria-valuenow={score}
        aria-valuemin={0}
        aria-valuemax={100}
        style={{ height }}
      >
        <div
          className={styles.fill}
          style={{
            width: `${pct}%`,
            background: color,
            boxShadow: `0 0 8px ${color}60`,
          }}
        />
      </div>
      {showLabel && (
        <span className={styles.label} style={{ color }}>
          {score.toFixed(1)}
        </span>
      )}
    </div>
  )
}
