import { useNavigate } from 'react-router-dom'
import { AlertCircle, ArrowUp, Copy } from 'lucide-react'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useArchivedAlerts } from '@/lib/archive-store'
import { ATTACK_ICON } from '@/lib/attack-display'

export function ArchivePage() {
  const navigate = useNavigate()
  const archived = useArchivedAlerts()

  return (
    <div className="flex flex-col gap-4">
      <section className="rounded-xl border border-border bg-card">
        <header className="border-b border-border px-5 py-3">
          <h2 className="text-base font-semibold text-foreground">
            Archived Attacks
          </h2>
        </header>

        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-[28%]">ID</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Attack Type</TableHead>
              <TableHead>Reason</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {archived.map((a) => {
              const Icon = ATTACK_ICON[a.attackType] ?? AlertCircle
              return (
                <TableRow
                  key={a.slug}
                  onClick={() => navigate(`/archive/${a.slug}`)}
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
                  <TableCell className="text-muted-foreground">
                    {a.date}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2 text-foreground">
                      <Icon className="h-4 w-4 text-muted-foreground" />
                      {a.attackType}
                    </div>
                  </TableCell>
                  <TableCell className="text-foreground">{a.reason}</TableCell>
                </TableRow>
              )
            })}
            {archived.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-muted-foreground"
                >
                  No archived attacks yet.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </section>

      {archived.length > 0 && (
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
      )}
    </div>
  )
}
