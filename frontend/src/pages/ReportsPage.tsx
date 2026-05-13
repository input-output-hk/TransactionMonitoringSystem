import { useMemo, useState } from 'react'
import {
  AlertCircle,
  ArrowUp,
  Banknote,
  Coins,
  Copy,
  ExternalLink,
  Fish,
  GitFork,
  Layers,
  PackageOpen,
  Repeat,
  ScrollText,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
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
  riskAlerts,
  type AttackType,
  type Severity,
} from '@/mocks/attacks'
import { cn } from '@/lib/utils'

const SEVERITY_VARIANT: Record<Severity, 'low' | 'medium' | 'high' | 'critical'> = {
  LOW: 'low',
  MEDIUM: 'medium',
  HIGH: 'high',
  CRITICAL: 'critical',
}

const ATTACK_ICON: Record<AttackType, React.ComponentType<{ className?: string }>> = {
  Sandwich: PackageOpen,
  Phishing: Fish,
  Circular: Repeat,
  'Multiple Sat': Layers,
  'Large Value': Banknote,
  'Large Datum': ScrollText,
  'Token Dust': Coins,
  'Front Running': GitFork,
  'Fake Token': AlertCircle,
}

// "DD.MM.YYYY, HH:mm" → Date
function parseAlertDate(s: string): Date {
  const [datePart, timePart = '00:00'] = s.split(', ')
  const [dd, mm, yyyy] = datePart.split('.')
  const [hh, min] = timePart.split(':')
  return new Date(Number(yyyy), Number(mm) - 1, Number(dd), Number(hh), Number(min))
}

export function ReportsPage() {
  const [startDate, setStartDate] = useState('2026-02-01')
  const [endDate, setEndDate] = useState('2026-03-01')
  const [attackFilter, setAttackFilter] = useState<string>('all')
  const [severityFilter, setSeverityFilter] = useState<string>('all')

  const filtered = useMemo(() => {
    const from = startDate ? new Date(startDate) : null
    const to = endDate ? new Date(endDate) : null
    if (to) to.setHours(23, 59, 59, 999)
    return riskAlerts.filter((a) => {
      if (attackFilter !== 'all' && a.attackType !== attackFilter) return false
      if (severityFilter !== 'all' && a.severity !== severityFilter) return false
      const d = parseAlertDate(a.date)
      if (from && d < from) return false
      if (to && d > to) return false
      return true
    })
  }, [attackFilter, severityFilter, startDate, endDate])

  return (
    <div className="flex flex-col gap-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-end gap-3">
        <DateField
          id="report-start"
          label="Start Date"
          value={startDate}
          onChange={setStartDate}
        />
        <DateField
          id="report-end"
          label="End Date"
          value={endDate}
          onChange={setEndDate}
        />

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="report-attack" className="text-xs text-foreground">
            Attack Type
          </Label>
          <Select value={attackFilter} onValueChange={setAttackFilter}>
            <SelectTrigger id="report-attack" className="h-11 w-[200px]">
              <SelectValue placeholder="All" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All</SelectItem>
              {ATTACK_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="report-severity" className="text-xs text-foreground">
            Severity Type
          </Label>
          <Select value={severityFilter} onValueChange={setSeverityFilter}>
            <SelectTrigger id="report-severity" className="h-11 w-[200px]">
              <SelectValue placeholder="All" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All</SelectItem>
              <SelectItem value="LOW">Low</SelectItem>
              <SelectItem value="MEDIUM">Medium</SelectItem>
              <SelectItem value="HIGH">High</SelectItem>
              <SelectItem value="CRITICAL">Critical</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="ml-auto pt-[22px]">
          <Button variant="outline" size="lg" className="h-11 gap-2">
            Export
            <ExternalLink className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Risk Alerts */}
      <section className="rounded-xl border border-border bg-card">
        <header className="border-b border-border px-5 py-3">
          <h2 className="text-base font-semibold text-foreground">Risk Alerts</h2>
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
            {filtered.map((a, i) => {
              const Icon = ATTACK_ICON[a.attackType] ?? AlertCircle
              return (
                <TableRow key={`${a.id}-${a.attackType}-${a.severity}-${i}`}>
                  <TableCell>
                    <div className="flex items-center gap-2 font-mono text-[13px] text-foreground">
                      <span>{a.id}</span>
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground"
                        title="Copy"
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
                  No risk alerts match the current filters.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </section>

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

function DateField({
  id,
  label,
  value,
  onChange,
}: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id} className="text-xs text-foreground">
        {label}
      </Label>
      <input
        id={id}
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          'flex h-11 w-[180px] items-center rounded-md border border-border bg-input/40 px-3 py-2 text-sm text-foreground transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background'
        )}
      />
    </div>
  )
}
