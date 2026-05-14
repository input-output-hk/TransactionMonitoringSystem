import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Copy,
  AlertCircle,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  ATTACK_TYPES,
  criticalAlertIdLong,
  latestBlocks,
  latestTransactions,
} from '@/mocks/attacks'
import { useActiveAlerts } from '@/lib/archive-store'
import { ATTACK_ICON, SEVERITY_VARIANT } from '@/lib/attack-display'
import { cn } from '@/lib/utils'

const SECONDARY_INFOS = [
  { label: 'TX / min', value: '12345' },
  { label: 'Pending', value: '12345' },
  { label: 'Critical (24h)', value: '12345' },
  { label: 'Avg Risk', value: '12345' },
]

export function AttacksPage() {
  const navigate = useNavigate()
  const activeAlerts = useActiveAlerts()
  const [attackFilter, setAttackFilter] = useState<string>('all')
  const [severityFilter, setSeverityFilter] = useState<string>('all')

  const filtered = useMemo(() => {
    return activeAlerts.filter((a) => {
      if (attackFilter !== 'all' && a.attackType !== attackFilter) return false
      if (severityFilter !== 'all' && a.severity !== severityFilter) return false
      return true
    })
  }, [activeAlerts, attackFilter, severityFilter])

  return (
    <div className="flex flex-col gap-4">
      {/* Top KPI row */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-7">
        <CriticalAlertCard />
        {SECONDARY_INFOS.map((info) => (
          <KpiCard key={info.label} label={info.label} value={info.value} />
        ))}
        <GraphBarCard />
      </div>

      {/* Risk Alerts */}
      <section className="rounded-xl border border-border bg-card">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3">
          <h2 className="text-base font-semibold text-foreground">Risk Alerts</h2>
          <div className="flex items-center gap-2">
            <Select value={attackFilter} onValueChange={setAttackFilter}>
              <SelectTrigger className="h-8 w-[160px]">
                <SelectValue placeholder="Attack Type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All attack types</SelectItem>
                {ATTACK_TYPES.map((t) => (
                  <SelectItem key={t} value={t}>
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={severityFilter} onValueChange={setSeverityFilter}>
              <SelectTrigger className="h-8 w-[160px]">
                <SelectValue placeholder="Severity Type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All severities</SelectItem>
                <SelectItem value="LOW">Low</SelectItem>
                <SelectItem value="MEDIUM">Medium</SelectItem>
                <SelectItem value="HIGH">High</SelectItem>
                <SelectItem value="CRITICAL">Critical</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </header>

        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-[42%]">ID</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Attack Type</TableHead>
              <TableHead className="text-right pr-6">Severity</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((a) => {
              const Icon = ATTACK_ICON[a.attackType] ?? AlertCircle
              return (
                <TableRow
                  key={a.slug}
                  onClick={() => navigate(`/attacks/${a.slug}`)}
                  className="cursor-pointer"
                >
                  <TableCell>
                    <div className="flex items-center gap-2 font-mono text-[13px] text-foreground">
                      <span>{a.id}</span>
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground"
                        title="Copy"
                        onClick={(e) => {
                          e.stopPropagation()
                          navigator.clipboard?.writeText(a.id)
                        }}
                      >
                        <Copy className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{a.date}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2 text-foreground">
                      <Icon className="h-4 w-4 text-muted-foreground" />
                      {a.attackType}
                    </div>
                  </TableCell>
                  <TableCell className="pr-6 text-right">
                    <Badge variant={SEVERITY_VARIANT[a.severity]}>
                      {a.severity}
                    </Badge>
                  </TableCell>
                </TableRow>
              )
            })}
            {filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground">
                  No alerts match the current filters.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3 text-xs text-muted-foreground">
          <div className="flex items-center gap-2">
            <span>Show Rows</span>
            <Select defaultValue="10">
              <SelectTrigger className="h-7 w-[64px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="10">10</SelectItem>
                <SelectItem value="25">25</SelectItem>
                <SelectItem value="50">50</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>Total Risk Alerts Shown: {filtered.length}</div>
          <div className="flex items-center gap-1">
            <IconBtn aria-label="First page">
              <ChevronsLeft className="h-3.5 w-3.5" />
            </IconBtn>
            <IconBtn aria-label="Previous page">
              <ChevronLeft className="h-3.5 w-3.5" />
            </IconBtn>
            <span className="px-2">Page 1 of 500</span>
            <IconBtn aria-label="Next page">
              <ChevronRight className="h-3.5 w-3.5" />
            </IconBtn>
            <IconBtn aria-label="Last page">
              <ChevronsRight className="h-3.5 w-3.5" />
            </IconBtn>
          </div>
        </footer>
      </section>

      {/* Latest Transactions + Latest Blocks */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <LatestList
          title="Latest Transactions"
          rows={latestTransactions.map((t) => ({
            primary: t.id,
            mono: true,
            middle: t.age,
            trailing: t.amountAda,
          }))}
        />
        <LatestList
          title="Latest Blocks"
          rows={latestBlocks.map((b) => ({
            primary: b.height,
            mono: false,
            middle: b.age,
            trailing: b.amountAda,
          }))}
        />
      </div>

      <div className="flex justify-end pt-2">
        <button
          type="button"
          onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowUp className="h-3.5 w-3.5" />
          Back to Top
        </button>
      </div>
    </div>
  )
}

function CriticalAlertCard() {
  return (
    <div className="md:col-span-2 rounded-xl border border-severity-critical-foreground/40 bg-card p-4 ring-1 ring-severity-critical/20">
      <div className="flex items-center gap-2 text-severity-critical-foreground">
        <AlertTriangle className="h-4 w-4" />
        <span className="text-sm font-semibold">New Critical Attack</span>
      </div>
      <div className="mt-2 flex items-center gap-2 font-mono text-xs text-muted-foreground">
        <span className="truncate">{criticalAlertIdLong}</span>
        <button
          type="button"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          title="Copy"
        >
          <Copy className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  )
}

function KpiCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="text-center text-sm font-semibold text-foreground">
        {label}
      </div>
      <div className="mt-2 text-center text-2xl font-bold text-brand">
        {value}
      </div>
    </div>
  )
}

function GraphBarCard() {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="text-sm font-semibold text-foreground">Graph Bar</div>
      <Sparkline className="mt-2 h-10 w-full" />
    </div>
  )
}

function Sparkline({ className }: { className?: string }) {
  // Decorative SVG mimicking the figma mini-chart silhouette
  return (
    <svg
      className={className}
      viewBox="0 0 120 40"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      preserveAspectRatio="none"
    >
      <path
        d="M0 32 L15 26 L25 30 L40 14 L55 28 L70 24 L85 8 L100 22 L120 18 L120 40 L0 40 Z"
        className="fill-brand/30"
      />
      <path
        d="M0 32 L15 26 L25 30 L40 14 L55 28 L70 24 L85 8 L100 22 L120 18"
        className="stroke-brand"
        strokeWidth="1.5"
        fill="none"
      />
    </svg>
  )
}

type ListRow = {
  primary: string
  mono: boolean
  middle: string
  trailing: string
}

function LatestList({ title, rows }: { title: string; rows: ListRow[] }) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <header className="border-b border-border px-5 py-3">
        <h2 className="text-base font-semibold text-foreground">{title}</h2>
      </header>
      <ul className="divide-y divide-border/60">
        {rows.map((r, i) => (
          <li
            key={i}
            className="grid grid-cols-3 items-center gap-2 px-5 py-3 text-sm"
          >
            <span
              className={cn(
                'truncate text-foreground',
                r.mono && 'font-mono text-[13px]'
              )}
            >
              {r.primary}
            </span>
            <span className="text-center text-muted-foreground">{r.middle}</span>
            <span className="text-right text-muted-foreground">{r.trailing}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function IconBtn({
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="h-7 w-7 text-muted-foreground hover:text-foreground"
      {...props}
    >
      {children}
    </Button>
  )
}
