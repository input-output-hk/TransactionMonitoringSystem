export const ATTACK_TYPES = [
  'Token Dust',
  'Large Value',
  'Large Datum',
  'Multiple Sat',
  'Front Running',
  'Sandwich',
  'Circular',
  'Fake Token',
  'Phishing',
] as const

export type AttackType = (typeof ATTACK_TYPES)[number]

export type Severity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'

export type RiskAlert = {
  id: string
  date: string
  attackType: AttackType
  severity: Severity
}

export type LatestTx = {
  id: string
  age: string
  amountAda: string
}

export type LatestBlock = {
  height: string
  age: string
  amountAda: string
}

function randHex(n: number) {
  const chars = 'abcdef0123456789'
  let out = ''
  for (let i = 0; i < n; i++) out += chars[Math.floor(Math.random() * chars.length)]
  return out
}

function id() {
  return `ADWED34${randHex(8)}...87TYHREH`
}

const ROWS: { type: AttackType; sev: Severity }[] = [
  { type: 'Sandwich', sev: 'LOW' },
  { type: 'Phishing', sev: 'HIGH' },
  { type: 'Circular', sev: 'CRITICAL' },
  { type: 'Multiple Sat', sev: 'HIGH' },
  { type: 'Large Value', sev: 'MEDIUM' },
  { type: 'Token Dust', sev: 'LOW' },
  { type: 'Front Running', sev: 'LOW' },
  { type: 'Token Dust', sev: 'CRITICAL' },
  { type: 'Token Dust', sev: 'MEDIUM' },
  { type: 'Circular', sev: 'LOW' },
]

export const riskAlerts: RiskAlert[] = ROWS.map((r) => ({
  id: id(),
  date: '25.02.2026, 22:49',
  attackType: r.type,
  severity: r.sev,
}))

export const latestTransactions: LatestTx[] = [
  { id: id(), age: '17 Seconds', amountAda: '0.19 ADA' },
  { id: id(), age: '25 Seconds', amountAda: '0.32 ADA' },
  { id: id(), age: '28 Seconds', amountAda: '0.28 ADA' },
  { id: id(), age: '35 Seconds', amountAda: '0.17 ADA' },
  { id: id(), age: '48 Seconds', amountAda: '0.45 ADA' },
]

export const latestBlocks: LatestBlock[] = [
  { height: '35889543', age: '10 Seconds', amountAda: '0.19 ADA' },
  { height: '57804321', age: '22 Seconds', amountAda: '0.32 ADA' },
  { height: '68906787', age: '24 Seconds', amountAda: '0.28 ADA' },
  { height: '16395038', age: '34 Seconds', amountAda: '0.17 ADA' },
  { height: '28394758', age: '42 Seconds', amountAda: '0.45 ADA' },
]

export const criticalAlertIdLong = `dfgsdfsd4rge4resvse${randHex(20)}terge4ge4er`

export const systemModules = [
  { name: 'Module 1', online: true },
  { name: 'Module 2', online: true },
  { name: 'Module 3', online: true },
  { name: 'Module 4', online: true },
] as const
