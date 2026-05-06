import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ArrowDownLeft, ArrowUpRight, ExternalLink } from 'lucide-react'
import { fetchTransaction, fetchScoreResult } from '../lib/api'
import { ATTACK_CLASSES } from '../types/api'
import RiskBadge from '../components/ui/RiskBadge'
import AttackClassBadge from '../components/ui/AttackClassBadge'
import ScoreBar from '../components/ui/ScoreBar'
import { useTmsStore } from '../store'
import styles from './TransactionDetail.module.css'

function cardanoscanUrl(network: string, txHash: string) {
  const sub = network === 'mainnet' ? '' : `${network}.`
  return `https://${sub}cardanoscan.io/transaction/${txHash}`
}

function ada(l: number) { return `${(l / 1_000_000).toLocaleString(undefined, { minimumFractionDigits: 6 })} ₳` }

export default function TransactionDetail() {
  const { hash } = useParams<{ hash: string }>()
  const network = useTmsStore((s) => s.network)

  const { data: tx, isLoading: txLoading } = useQuery({
    queryKey: ['tx', hash],
    queryFn: () => fetchTransaction(hash!),
    enabled: !!hash,
  })

  const { data: score } = useQuery({
    queryKey: ['score', hash],
    queryFn: () => fetchScoreResult(hash!),
    enabled: !!hash,
  })

  if (txLoading) {
    return (
      <div className={styles.root}>
        <div className={styles.loading}>
          <div className="skeleton" style={{ width: 260, height: 20 }} />
          <div className="skeleton" style={{ width: 160, height: 14 }} />
        </div>
      </div>
    )
  }

  if (!tx) {
    return (
      <div className={styles.root}>
        <div className={styles.notFound}>Transaction not found</div>
      </div>
    )
  }

  return (
    <div className={styles.root}>
      {/* Breadcrumb */}
      <Link to="/transactions" className={styles.back}>
        <ArrowLeft size={14} />
        Back to transactions
      </Link>

      {/* Header */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <h2 className={styles.hashFull}>{tx.tx_hash}</h2>
          <div className={styles.metaRow}>
            <span className={styles.metaItem}>
              Block <span className={styles.monoVal}>#{tx.block_height.toLocaleString()}</span>
            </span>
            <span className={styles.metaDot}>·</span>
            <span className={styles.metaItem}>
              Slot <span className={styles.monoVal}>{tx.slot.toLocaleString()}</span>
            </span>
            <span className={styles.metaDot}>·</span>
            <span className={styles.metaItem}>
              {new Date(tx.timestamp).toLocaleString()}
            </span>
            <a
              href={cardanoscanUrl(network, tx.tx_hash)}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.explorerLink}
            >
              <ExternalLink size={12} /> Cardanoscan
            </a>
          </div>
        </div>
        {score && <RiskBadge band={score.risk_band} score={score.max_score} size="lg" />}
      </div>

      {/* Main grid */}
      <div className={styles.grid}>
        {/* Left: TX info */}
        <div className={styles.left}>
          {/* Summary */}
          <div className={styles.card}>
            <h3 className={styles.cardTitle}>Summary</h3>
            <div className={styles.summaryGrid}>
              <div className={styles.summaryRow}>
                <span className={styles.summaryKey}>Fee</span>
                <span className={styles.summaryVal}>{ada(tx.fee)}</span>
              </div>
              <div className={styles.summaryRow}>
                <span className={styles.summaryKey}>Total Output</span>
                <span className={styles.summaryVal}>{ada(tx.total_output_value)}</span>
              </div>
              {tx.total_input_value && (
                <div className={styles.summaryRow}>
                  <span className={styles.summaryKey}>Total Input</span>
                  <span className={styles.summaryVal}>{ada(tx.total_input_value)}</span>
                </div>
              )}
              <div className={styles.summaryRow}>
                <span className={styles.summaryKey}>Inputs</span>
                <span className={styles.summaryVal}>{tx.input_count}</span>
              </div>
              <div className={styles.summaryRow}>
                <span className={styles.summaryKey}>Outputs</span>
                <span className={styles.summaryVal}>{tx.output_count}</span>
              </div>
              {tx.deposit !== null && (
                <div className={styles.summaryRow}>
                  <span className={styles.summaryKey}>Deposit</span>
                  <span className={styles.summaryVal}>{ada(tx.deposit ?? 0)}</span>
                </div>
              )}
              <div className={styles.summaryRow}>
                <span className={styles.summaryKey}>Network</span>
                <span className={styles.summaryVal}>{tx.network}</span>
              </div>
            </div>
          </div>

          {/* Inputs */}
          <div className={styles.card}>
            <h3 className={styles.cardTitle}>
              <ArrowDownLeft size={14} style={{ color: 'var(--risk-low)' }} />
              Inputs ({tx.input_count})
            </h3>
            <div className={styles.utxoList}>
              {tx.inputs.map((inp, i) => (
                <div key={i} className={styles.utxo}>
                  <div className={styles.utxoAddr}>{inp.address.slice(0, 20)}…{inp.address.slice(-8)}</div>
                  <div className={styles.utxoRight}>
                    <span className={styles.utxoAda}>{ada(inp.amount)}</span>
                    {inp.is_collateral && <span className={styles.tag}>collateral</span>}
                    {inp.is_reference && <span className={styles.tag}>reference</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Outputs */}
          <div className={styles.card}>
            <h3 className={styles.cardTitle}>
              <ArrowUpRight size={14} style={{ color: 'var(--risk-moderate)' }} />
              Outputs ({tx.output_count})
            </h3>
            <div className={styles.utxoList}>
              {tx.outputs.map((out, i) => (
                <div key={i} className={styles.utxo}>
                  <div className={styles.utxoAddr}>{out.address.slice(0, 20)}…{out.address.slice(-8)}</div>
                  <div className={styles.utxoRight}>
                    <span className={styles.utxoAda}>{ada(out.amount)}</span>
                    {Object.keys(out.assets ?? {}).length > 0 && (
                      <span className={styles.tag}>{Object.keys(out.assets ?? {}).length} asset{Object.keys(out.assets ?? {}).length > 1 ? 's' : ''}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Right: Score breakdown */}
        {score && (
          <div className={styles.right}>
            <div className={styles.card}>
              <h3 className={styles.cardTitle}>Risk Analysis</h3>

              <div className={styles.topClass}>
                <AttackClassBadge cls={score.max_class} />
                <span className={styles.topClassSub}>Dominant attack class</span>
              </div>

              <div className={styles.allScores}>
                {ATTACK_CLASSES.map((cls) => {
                  const s = score.scores[cls]
                  if (s === -1) return (
                    <div key={cls} className={`${styles.scoreRow} ${styles.scoreRowNA}`}>
                      <AttackClassBadge cls={cls} size="sm" />
                      <span className={styles.naLabel}>N/A</span>
                    </div>
                  )
                  return (
                    <div key={cls} className={styles.scoreRow}>
                      <AttackClassBadge cls={cls} size="sm" />
                      <ScoreBar score={s} height={5} showLabel />
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Sub-score breakdown for top class */}
            <div className={styles.card}>
              <h3 className={styles.cardTitle}>Sub-Score Detail</h3>
              <div className={styles.subTitle}>
                <AttackClassBadge cls={score.max_class} size="sm" />
              </div>
              {Object.entries(score.sub_scores[score.max_class] ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([key, val]) => (
                  <div key={key} className={styles.subRow}>
                    <span className={styles.subKey}>{key.replace(/_/g, ' ')}</span>
                    <div className={styles.subBar}>
                      <div
                        className={styles.subFill}
                        style={{ width: `${val * 100}%` }}
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
