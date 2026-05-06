import type { ReactNode } from 'react'
import styles from './KPICard.module.css'

interface Props {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: string
  icon?: ReactNode
  pulse?: boolean
}

export default function KPICard({ label, value, sub, accent, icon, pulse }: Props) {
  return (
    <div
      className={styles.card}
      style={accent ? { '--card-accent': accent } as React.CSSProperties : undefined}
    >
      <div className={styles.header}>
        <span className={styles.label}>{label}</span>
        {icon && <span className={styles.icon}>{icon}</span>}
        {pulse && <span className={styles.pulse} />}
      </div>
      <div className={styles.value}>{value}</div>
      {sub && <div className={styles.sub}>{sub}</div>}
    </div>
  )
}
