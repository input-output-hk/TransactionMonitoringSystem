import type { AttackClass } from '../../types/api'
import { ATTACK_CLASS_LABELS } from '../../types/api'
import styles from './AttackClassBadge.module.css'

const CLASS_COLORS: Record<AttackClass, string> = {
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

interface Props {
  cls: AttackClass
  size?: 'sm' | 'md'
}

export default function AttackClassBadge({ cls, size = 'md' }: Props) {
  const color = CLASS_COLORS[cls]
  return (
    <span
      className={`${styles.badge} ${styles[size]}`}
      style={{
        '--cls-color': color,
        '--cls-bg': `${color}18`,
        '--cls-border': `${color}30`,
      } as React.CSSProperties}
    >
      {ATTACK_CLASS_LABELS[cls]}
    </span>
  )
}
