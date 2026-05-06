/* Types aligned with the TMS backend models (backend/app/models/transaction.py) */

export type Network = 'mainnet' | 'preprod' | 'preview'

export type LifecycleStatus = 'PENDING' | 'CONFIRMED' | 'ROLLED_BACK' | 'DROPPED'

export type RiskBand = 'Low' | 'Moderate' | 'High' | 'Critical'

export type AttackClass =
  | 'token_dust'
  | 'large_value'
  | 'large_datum'
  | 'multiple_sat'
  | 'front_running'
  | 'sandwich'
  | 'circular'
  | 'fake_token'
  | 'phishing'

export const ATTACK_CLASSES: AttackClass[] = [
  'token_dust',
  'large_value',
  'large_datum',
  'multiple_sat',
  'front_running',
  'sandwich',
  'circular',
  'fake_token',
  'phishing',
]

export const ATTACK_CLASS_LABELS: Record<AttackClass, string> = {
  token_dust: 'Token Dust',
  large_value: 'Large Value',
  large_datum: 'Large Datum',
  multiple_sat: 'Multi-Sat',
  front_running: 'Front Running',
  sandwich: 'Sandwich',
  circular: 'Circular',
  fake_token: 'Fake Token',
  phishing: 'Phishing',
}

export const RISK_BAND_ORDER: RiskBand[] = ['Low', 'Moderate', 'High', 'Critical']

export interface TransactionInput {
  tx_hash: string
  output_index: number
  address: string
  amount: number
  assets: Record<string, number>
  is_reference: boolean
  is_collateral: boolean
}

export interface TransactionOutput {
  output_index: number
  address: string
  amount: number
  assets: Record<string, number>
  is_collateral: boolean
}

export interface NormalizedTransaction {
  tx_hash: string
  network: Network
  slot: number
  block_height: number
  block_hash: string
  block_index: number
  timestamp: string
  fee: number
  deposit: number | null
  inputs: TransactionInput[]
  outputs: TransactionOutput[]
  input_count: number
  output_count: number
  total_input_value: number | null
  total_output_value: number
  addresses: string[]
  metadata: Record<string, unknown> | null
}

export interface ClassScoreResult {
  tx_hash: string
  network: Network
  scores: Record<AttackClass, number>
  max_score: number
  max_class: AttackClass
  risk_band: RiskBand
  sub_scores: Record<AttackClass, Record<string, number>>
  analysis_version: string
  analyzed_at: string
  fee: number | null
  output_count: number | null
}

export interface TransactionLifecycleEvent {
  tx_id: string
  network: Network
  status: LifecycleStatus
  first_seen_at: string | null
  confirmed_at: string | null
  rolled_back_at: string | null
  block_hash: string | null
  slot: number | null
  height: number | null
  latency_ms: number | null
}

/* API response wrappers */

export interface PaginatedResponse<T> {
  items: T[]
  next_cursor: string | null
  total?: number
}

export interface TransactionStats {
  count: number
  total_volume: number
  avg_value: number
  total_fees: number
  network: Network
}

export interface AnalysisStats {
  band_counts: Record<RiskBand, number>
  class_distributions: Record<AttackClass, { mean: number; p95: number; count: number }>
  total_analyzed: number
}

export interface LifecycleStats {
  pending: number
  confirmed: number
  rolled_back: number
  dropped: number
  avg_latency_ms: number | null
  rollback_rate: number
}

export interface HealthDetail {
  status: string
  pipeline_state: string
  ogmios_connected: boolean
  ws_connections: number
  last_block_at: string | null
  last_processed_slot: number | null
  tip_slot: number | null
  uptime_seconds: number
}

/* WebSocket event types */
export interface WsLifecycleEvent {
  type: 'lifecycle'
  data: TransactionLifecycleEvent
  timestamp: string
}

export type WsEvent = WsLifecycleEvent
