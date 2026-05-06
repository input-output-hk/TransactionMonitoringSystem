import { useState } from 'react'
import { Save, RotateCcw, Info } from 'lucide-react'
import { ATTACK_CLASSES, ATTACK_CLASS_LABELS, type AttackClass } from '../types/api'
import AttackClassBadge from '../components/ui/AttackClassBadge'
import styles from './Detection.module.css'

/* Default weight configs mirroring detection.yaml */
type WeightConfig = Record<string, number>
type GateConfig = Record<string, number | string>

interface ClassConfig {
  weights: WeightConfig
  gates: GateConfig
  reason_threshold: number
}

const DEFAULTS: Record<AttackClass, ClassConfig> = {
  token_dust: {
    weights: { bytes: 0.35, assets: 0.35, ada_inv: 0.15, recurrence: 0.15 },
    gates: { min_token_count: 2 },
    reason_threshold: 0.5,
  },
  large_value: {
    weights: { digits: 0.55, bytes: 0.20, ada_inv: 0.15, recurrence: 0.10 },
    gates: {},
    reason_threshold: 0.5,
  },
  large_datum: {
    weights: { datum_bytes: 0.50, datum_ratio: 0.35, value_cbor_inv: 0.05, recurrence: 0.10 },
    gates: { min_datum_bytes: 6000 },
    reason_threshold: 0.5,
  },
  multiple_sat: {
    weights: { extraction: 0.42, exunits_inv: 0.28, inputs: 0.16, recurrence: 0.14 },
    gates: {},
    reason_threshold: 0.5,
  },
  front_running: {
    // outcome·0.35 delta·0.30 recurrence·0.25 structure·0.10
    weights: { outcome: 0.35, delta: 0.30, recurrence: 0.25, structure: 0.10 },
    gates: { min_recurrence_wins: 3, high_band_cap: 79.0 },
    reason_threshold: 0.8,   // per reason_thresholds.outcome in yaml
  },
  sandwich: {
    // link·0.30 rate·0.30 impact·0.20 profit·0.10 recurrence·0.10
    weights: { link: 0.30, rate: 0.30, impact: 0.20, profit: 0.10, recurrence: 0.10 },
    gates: { window_slots: 5, min_profit_lovelace: 200000, high_band_cap: 79.0 },
    reason_threshold: 0.5,
  },
  circular: {
    // amount·0.30 recurrence·0.30 entropy·0.20 auxiliary·0.10 speed·0.10
    weights: { amount: 0.30, recurrence: 0.30, entropy: 0.20, auxiliary: 0.10, speed: 0.10 },
    gates: { min_cycle_length: 2, max_cycle_length: 6, fee_tolerance_multiplier: 4.0 },
    reason_threshold: 0.5,
  },
  fake_token: {
    // overall: identity·0.60 distribution·0.40
    // identity: name·0.40 unicode·0.35 cip25·0.25
    // distribution: recipients·0.40 ratio·0.30 policy_age·0.20 recurrence·0.10
    weights: { 'overall.identity': 0.60, 'overall.distribution': 0.40 },
    gates: { similarity_threshold: 0.80 },
    reason_threshold: 0.3,   // per reason_thresholds.name in yaml
  },
  phishing: {
    // overall: content·0.65 delivery·0.35
    // content: blacklist·0.30 domain·0.30 social·0.40
    // delivery: recipients·0.35 url_recur·0.25 targeting·0.25 recurrence·0.15
    weights: { 'overall.content': 0.65, 'overall.delivery': 0.35 },
    gates: { critical_threshold: 0.60 },
    reason_threshold: 0.5,
  },
}

const CLASS_DESCRIPTIONS: Record<AttackClass, string> = {
  token_dust:    'Low-value token bloat attacks that stuff UTxOs with many small-value native assets to inflate CBOR size.',
  large_value:   'Extreme token quantity attacks using near-max int64 values to exploit ledger or off-chain processing.',
  large_datum:   'Oversized datum state bloat attacks. Script outputs carrying abnormally large inline datums.',
  multiple_sat:  'Multiple script satisfaction exploitation. Transactions that spend the same validator multiple times to extract value.',
  front_running: 'Transaction ordering attacks. Exploiting mempool visibility to insert a tx ahead of a target.',
  sandwich:      'Sandwich / MEV extraction. Surrounding a victim tx with buy/sell to profit from price impact.',
  circular:      'Circular transfer laundering. Fund flows that loop back to the originator to obscure value movement.',
  fake_token:    'Deceptive token impersonation using visually similar names or unicode lookalikes to mislead users.',
  phishing:      'Phishing metadata in on-chain transaction labels (674/721) directing users to malicious domains.',
}

/* Classes whose weights have a two-level hierarchy in the YAML */
const NESTED_CLASSES = new Set<AttackClass>(['fake_token', 'phishing'])

const NESTED_WEIGHT_DETAIL: Partial<Record<AttackClass, string>> = {
  fake_token: `    # Sub-group weights (identity: name·0.40 unicode·0.35 cip25·0.25)
    # Sub-group weights (distribution: recipients·0.40 ratio·0.30 policy_age·0.20 recurrence·0.10)
    # Edit sub-group weights directly in detection.yaml`,
  phishing: `    # Sub-group weights (content: blacklist·0.30 domain·0.30 social·0.40)
    # Sub-group weights (delivery: recipients·0.35 url_recur·0.25 targeting·0.25 recurrence·0.15)
    # Edit sub-group weights directly in detection.yaml`,
}

function yamlPreview(cls: AttackClass, cfg: ClassConfig): string {
  const isNested = NESTED_CLASSES.has(cls)
  const weightsBlock = isNested
    ? Object.entries(cfg.weights)
        .map(([k, v]) => {
          const [group, key] = k.split('.')
          return `      ${group}:\n        ${key}: ${v.toFixed(2)}`
        })
        .join('\n')
    : Object.entries(cfg.weights).map(([k, v]) => `      ${k}: ${v.toFixed(2)}`).join('\n')

  const gateBlock = Object.keys(cfg.gates).length > 0
    ? `\n    gate:\n${Object.entries(cfg.gates).map(([k, v]) => `      ${k}: ${v}`).join('\n')}`
    : ''

  const nestedNote = NESTED_WEIGHT_DETAIL[cls] ? `\n${NESTED_WEIGHT_DETAIL[cls]}` : ''

  return `  ${cls}:
    weights:
${weightsBlock}${nestedNote}${gateBlock}
    reason_threshold: ${cfg.reason_threshold.toFixed(2)}`
}

function WeightSlider({
  label, value, onChange,
}: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className={styles.sliderRow}>
      <span className={styles.sliderLabel}>{label.replace(/_/g, ' ')}</span>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className={styles.slider}
      />
      <span className={styles.sliderVal}>{value.toFixed(2)}</span>
    </div>
  )
}

export default function Detection() {
  const [activeClass, setActiveClass] = useState<AttackClass>('multiple_sat')
  const [configs, setConfigs] = useState<Record<AttackClass, ClassConfig>>({ ...DEFAULTS })
  const [saved, setSaved] = useState(false)

  const cfg = configs[activeClass]

  function updateWeight(key: string, val: number) {
    setConfigs((prev) => ({
      ...prev,
      [activeClass]: {
        ...prev[activeClass],
        weights: { ...prev[activeClass].weights, [key]: val },
      },
    }))
  }

  function updateGate(key: string, val: string | number) {
    setConfigs((prev) => ({
      ...prev,
      [activeClass]: {
        ...prev[activeClass],
        gates: { ...prev[activeClass].gates, [key]: val },
      },
    }))
  }

  function handleSave() {
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  function handleReset() {
    setConfigs((prev) => ({
      ...prev,
      [activeClass]: { ...DEFAULTS[activeClass] },
    }))
  }

  const weightSum = Object.values(cfg.weights).reduce((a, b) => a + b, 0)
  const sumOk = Math.abs(weightSum - 1.0) < 0.01

  return (
    <div className={styles.root}>
      {/* Sidebar tabs */}
      <div className={styles.tabs}>
        {ATTACK_CLASSES.map((cls) => (
          <button
            key={cls}
            className={`${styles.tab} ${cls === activeClass ? styles.tabActive : ''}`}
            onClick={() => setActiveClass(cls)}
          >
            <AttackClassBadge cls={cls} size="sm" />
          </button>
        ))}
      </div>

      {/* Editor */}
      <div className={styles.editor}>
        <div className={styles.editorHeader}>
          <div>
            <AttackClassBadge cls={activeClass} />
            <p className={styles.editorDesc}>{CLASS_DESCRIPTIONS[activeClass]}</p>
          </div>
          <div className={styles.editorActions}>
            <button className={styles.resetBtn} onClick={handleReset}>
              <RotateCcw size={13} /> Reset
            </button>
            <button className={styles.saveBtn} onClick={handleSave} disabled={!sumOk}>
              <Save size={13} />
              {saved ? 'Saved!' : 'Save Config'}
            </button>
          </div>
        </div>

        {/* Weights */}
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h3 className={styles.sectionTitle}>Feature Weights</h3>
            <span className={styles.sumBadge} data-ok={sumOk}>
              Sum: {weightSum.toFixed(2)} {sumOk ? '✓' : '— must equal 1.00'}
            </span>
          </div>
          <div className={styles.sliderGrid}>
            {Object.entries(cfg.weights).map(([key, val]) => (
              <WeightSlider key={key} label={key} value={val} onChange={(v) => updateWeight(key, v)} />
            ))}
          </div>
          <div className={styles.weightViz}>
            {Object.entries(cfg.weights).map(([key, val]) => (
              <div
                key={key}
                className={styles.weightBar}
                style={{ width: `${val * 100}%` }}
                title={`${key}: ${(val * 100).toFixed(0)}%`}
              />
            ))}
          </div>
        </div>

        {/* Gates */}
        {Object.keys(cfg.gates).length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeader}>
              <h3 className={styles.sectionTitle}>Gate Conditions</h3>
              <span className={styles.infoIcon} title="Hard prerequisites. If the gate fails, the scorer returns -1 (not applicable).">
                <Info size={13} />
              </span>
            </div>
            <div className={styles.gateGrid}>
              {Object.entries(cfg.gates).map(([key, val]) => (
                <div key={key} className={styles.gateRow}>
                  <label className={styles.gateLabel}>{key.replace(/_/g, ' ')}</label>
                  <input
                    className={styles.gateInput}
                    type="number"
                    value={Number(val)}
                    step={typeof val === 'number' && val < 1 ? 0.01 : 1}
                    onChange={(e) => updateGate(key, parseFloat(e.target.value))}
                  />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Reason threshold */}
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h3 className={styles.sectionTitle}>Reason Threshold</h3>
            <span className={styles.infoIcon} title="Minimum sub-score to include in the alert explanation">
              <Info size={13} />
            </span>
          </div>
          <WeightSlider
            label="reason_threshold"
            value={cfg.reason_threshold}
            onChange={(v) =>
              setConfigs((prev) => ({
                ...prev,
                [activeClass]: { ...prev[activeClass], reason_threshold: v },
              }))
            }
          />
        </div>

        {/* YAML preview */}
        <div className={styles.section}>
          <h3 className={styles.sectionTitle}>Config Preview (YAML)</h3>
          <pre className={styles.yaml}>{yamlPreview(activeClass, cfg)}</pre>
        </div>
      </div>
    </div>
  )
}
