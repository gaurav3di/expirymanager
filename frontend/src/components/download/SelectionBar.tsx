import { cn } from 'cn'

import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import type { ExpiryRow } from '@/components/download/ExpirySelectTable'
import { formatCompact, formatInteger, pluralise } from '@/lib/format'

// The sticky summary of what is ticked.
//
// It exists because the selection outlives the page: rows ticked on page one are invisible once
// page two loads, and a Download button that prices six expiries while two are on screen has to
// say so. Every figure here is summed from the selected rows themselves, so the bar cannot claim
// a different selection than the one the plan request will carry.

export interface SelectionSummary {
  expiries: number
  contracts: number
  futures: number
  options: number
  /** Expiries whose contracts have never been discovered. Each one costs a discovery request
   *  before any candle request can be planned, so it is named rather than folded in. */
  undiscovered: number
  contractsWithData: number
  contractsWithoutData: number
  rows: number
  firstExpiry: string | null
  lastExpiry: string | null
}

export function summariseSelection(rows: readonly ExpiryRow[]): SelectionSummary {
  const summary: SelectionSummary = {
    expiries: rows.length,
    contracts: 0,
    futures: 0,
    options: 0,
    undiscovered: 0,
    contractsWithData: 0,
    contractsWithoutData: 0,
    rows: 0,
    firstExpiry: null,
    lastExpiry: null,
  }

  for (const row of rows) {
    summary.contracts += row.contract_count ?? 0
    summary.futures += row.futures_count ?? 0
    summary.options += row.options_count ?? 0
    summary.contractsWithData += row.coverage.contracts_with_data
    summary.contractsWithoutData += row.coverage.contracts_without_data
    summary.rows += row.coverage.rows
    if (row.contracts_discovered_at === null) {
      summary.undiscovered += 1
    }
    if (summary.firstExpiry === null || row.expiry_date < summary.firstExpiry) {
      summary.firstExpiry = row.expiry_date
    }
    if (summary.lastExpiry === null || row.expiry_date > summary.lastExpiry) {
      summary.lastExpiry = row.expiry_date
    }
  }
  return summary
}

/** The one line under the counts. Says the span, and says what is not yet discovered, because
 *  that is the difference between a sheet that can be priced and one that cannot. */
export function summaryLine(summary: SelectionSummary): string {
  if (summary.expiries === 0) {
    return 'Nothing selected'
  }
  const parts: string[] = []
  if (summary.firstExpiry && summary.lastExpiry) {
    parts.push(
      summary.firstExpiry === summary.lastExpiry
        ? summary.firstExpiry
        : summary.firstExpiry + ' to ' + summary.lastExpiry,
    )
  }
  if (summary.contracts > 0) {
    parts.push(
      formatInteger(summary.contracts) +
        ' contracts, ' +
        formatInteger(summary.contractsWithData) +
        ' already hold data',
    )
  }
  if (summary.undiscovered > 0) {
    parts.push(
      pluralise(summary.undiscovered, 'expiry', 'expiries') +
        ' need contract discovery first',
    )
  }
  return parts.join('. ')
}

export interface SelectionBarProps {
  rows: readonly ExpiryRow[]
  onClear: () => void
  onDownload: () => void
  /** Disables Download and says why, for the states the sheet itself cannot fix, such as a
   *  broker token that has gone. */
  disabledReason?: string | null
  className?: string
}

export function SelectionBar({
  rows,
  onClear,
  onDownload,
  disabledReason = null,
  className,
}: SelectionBarProps) {
  if (rows.length === 0) {
    return null
  }
  const summary = summariseSelection(rows)

  return (
    <div
      className={cn(
        // Sticky to the bottom of the screen area rather than fixed to the viewport, so it sits
        // inside the content column and never covers the sidebar.
        'sticky bottom-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-t bg-background/95 px-5 py-3 backdrop-blur',
        className,
      )}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-medium tabular-nums">
            {pluralise(summary.expiries, 'expiry', 'expiries')} selected
          </span>
          {summary.undiscovered > 0 ? (
            <Badge variant="outline" className="text-[0.65rem]">
              {formatInteger(summary.undiscovered)} need discovery
            </Badge>
          ) : null}
        </div>
        <span className="text-xs text-muted-foreground">{summaryLine(summary)}</span>
      </div>

      <dl className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-xs">
        <div className="flex items-baseline gap-1.5">
          <dt className="text-muted-foreground">Contracts</dt>
          <dd className="tabular-nums">{formatInteger(summary.contracts)}</dd>
        </div>
        <div className="flex items-baseline gap-1.5">
          <dt className="text-muted-foreground">Held</dt>
          <dd className="tabular-nums">{formatInteger(summary.contractsWithData)}</dd>
        </div>
        <div className="flex items-baseline gap-1.5">
          <dt className="text-muted-foreground">Rows stored</dt>
          <dd className="tabular-nums">{formatCompact(summary.rows)}</dd>
        </div>
      </dl>

      <div className="ml-auto flex items-center gap-2">
        {disabledReason ? (
          <span className="text-xs text-destructive">{disabledReason}</span>
        ) : null}
        <Button type="button" variant="ghost" size="sm" onClick={onClear}>
          Clear
        </Button>
        <Button type="button" size="sm" onClick={onDownload} disabled={disabledReason !== null}>
          Download
        </Button>
      </div>
    </div>
  )
}

export default SelectionBar
