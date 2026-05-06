import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { Link } from 'react-router-dom'
import styles from './TxHash.module.css'

interface Props {
  hash: string
  link?: boolean
  chars?: number
}

export default function TxHash({ hash, link = false, chars = 8 }: Props) {
  const [copied, setCopied] = useState(false)

  if (!hash) return null

  const short = `${hash.slice(0, chars)}…${hash.slice(-4)}`

  async function handleCopy(e: React.MouseEvent) {
    e.preventDefault()
    e.stopPropagation()
    await navigator.clipboard.writeText(hash)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  const inner = (
    <span className={styles.text}>{short}</span>
  )

  return (
    <span className={styles.wrap}>
      {link ? (
        <Link to={`/transactions/${hash}`} className={styles.link}>
          {inner}
        </Link>
      ) : inner}
      <button
        className={styles.copy}
        onClick={handleCopy}
        aria-label="Copy transaction hash"
        title={hash}
      >
        {copied
          ? <Check size={11} color="var(--risk-low)" />
          : <Copy size={11} />
        }
      </button>
    </span>
  )
}
