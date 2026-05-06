import styles from './LiveDot.module.css'

type Status = 'connecting' | 'connected' | 'disconnected' | 'error'

interface Props {
  status: Status
  size?: number
}

const STATUS_COLOR: Record<Status, string> = {
  connected:    'var(--risk-low)',
  connecting:   'var(--risk-moderate)',
  disconnected: 'var(--risk-none)',
  error:        'var(--risk-critical)',
}

export default function LiveDot({ status, size = 8 }: Props) {
  const color = STATUS_COLOR[status]
  return (
    <span
      className={styles.wrap}
      style={{ '--dot-color': color, '--dot-size': `${size}px` } as React.CSSProperties}
      aria-label={status}
    >
      <span className={styles.dot} />
      {status === 'connected' && <span className={styles.ring} />}
    </span>
  )
}
