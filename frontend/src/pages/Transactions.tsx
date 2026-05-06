import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Search, ArrowUpRight, ArrowDownLeft } from 'lucide-react'
import { fetchTransactions, fetchTransactionStats } from '../lib/api'
import TxHash from '../components/ui/TxHash'
import styles from './Transactions.module.css'

function ada(l: number) {
  return `${(l / 1_000_000).toFixed(2)} ₳`
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function timeAgo(iso: string): string {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return new Date(iso).toLocaleDateString()
}

export default function Transactions() {
  const [search, setSearch] = useState('')
  const [submitted, setSubmitted] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['transactions', submitted],
    queryFn: () => fetchTransactions({ limit: 25, address: submitted || undefined }),
    refetchInterval: 15_000,
  })

  const { data: stats } = useQuery({
    queryKey: ['txStats'],
    queryFn: fetchTransactionStats,
    refetchInterval: 60_000,
  })

  function handleSearch(e: React.FormEvent) {
    e.preventDefault()
    setSubmitted(search.trim())
  }

  const txs = data?.items ?? []

  return (
    <div className={styles.root}>
      {/* Stats strip */}
      <div className={styles.statsStrip}>
        <div className={styles.stat}>
          <span className={styles.statLabel}>Total (24h)</span>
          <span className={styles.statValue}>{fmt(stats?.count ?? 0)}</span>
        </div>
        <div className={styles.statDiv} />
        <div className={styles.stat}>
          <span className={styles.statLabel}>Volume</span>
          <span className={styles.statValue}>{ada(stats?.total_volume ?? 0)}</span>
        </div>
        <div className={styles.statDiv} />
        <div className={styles.stat}>
          <span className={styles.statLabel}>Avg Value</span>
          <span className={styles.statValue}>{ada(stats?.avg_value ?? 0)}</span>
        </div>
        <div className={styles.statDiv} />
        <div className={styles.stat}>
          <span className={styles.statLabel}>Total Fees</span>
          <span className={styles.statValue}>{ada(stats?.total_fees ?? 0)}</span>
        </div>
      </div>

      {/* Search */}
      <form className={styles.searchBar} onSubmit={handleSearch}>
        <Search size={14} color="var(--text-muted)" />
        <input
          className={styles.searchInput}
          placeholder="Search by address or transaction hash…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button type="submit" className={styles.searchBtn}>
          Search
        </button>
        {submitted && (
          <button
            type="button"
            className={styles.clearBtn}
            onClick={() => { setSearch(''); setSubmitted('') }}
          >
            Clear
          </button>
        )}
      </form>

      {/* Table */}
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Tx Hash</th>
              <th>Block</th>
              <th>Time</th>
              <th>In / Out</th>
              <th>Fee</th>
              <th>Value Out</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {isLoading && Array.from({ length: 10 }).map((_, i) => (
              <tr key={i}>
                {Array.from({ length: 6 }).map((_, j) => (
                  <td key={j}>
                    <div className="skeleton" style={{ height: 13, width: j === 0 ? 110 : j === 2 ? 60 : 80 }} />
                  </td>
                ))}
                <td />
              </tr>
            ))}
            {!isLoading && txs.length === 0 && (
              <tr>
                <td colSpan={7} className={styles.empty}>No transactions found</td>
              </tr>
            )}
            {txs.map((tx) => (
              <tr key={tx.tx_hash} className={styles.row}>
                <td><TxHash hash={tx.tx_hash} link chars={10} /></td>
                <td className={styles.monoCell}>#{tx.block_height.toLocaleString()}</td>
                <td className={styles.timeCell}>{timeAgo(tx.timestamp)}</td>
                <td className={styles.ioCell}>
                  <span className={styles.ioIn}>
                    <ArrowDownLeft size={10} /> {tx.input_count}
                  </span>
                  <span className={styles.ioSep}>/</span>
                  <span className={styles.ioOut}>
                    <ArrowUpRight size={10} /> {tx.output_count}
                  </span>
                </td>
                <td className={styles.monoCell}>{ada(tx.fee)}</td>
                <td className={styles.monoCell}>{ada(tx.total_output_value)}</td>
                <td>
                  <Link to={`/transactions/${tx.tx_hash}`} className={styles.detailLink}>
                    Detail →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
