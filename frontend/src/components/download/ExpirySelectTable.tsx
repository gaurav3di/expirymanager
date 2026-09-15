import { useMemo } from 'react'
import type { ReactNode } from 'react'
import type { ColumnDef, OnChangeFn, RowSelectionState } from '@tanstack/react-table'
import { cn } from 'cn'

import { CoverageBar } from '@/components/common/CoverageBar'
import type { CoverageBarProps } from '@/components/common/CoverageBar'
import { DataTable } from '@/components/common/DataTable'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
import type { AppTableFeatures } from '@/lib/tables/features'
import { formatCompact, formatInteger, formatStrike } from '@/lib/format'

// The expiry table, with row selection, and the reconciliation that makes a selection mean
// something across cursor pages.
//
// Two facts about this screen shape everything below.
//
// 1. The list is server driven and cursor paged. The table only ever holds one page, so the
//    selection cannot live in the table: a user who ticks four expiries on page one, pages
//    forward and ticks two more has a selection of six, and the table can see two of them.
//    `reconcileSelection` is where that is kept straight, and it is a pure function so the case
//    that breaks it is a test and not a bug report.
// 2. The coverage figure is read from the ledger, never recomputed from candles. What the ledger
//    can answer honestly at the expiry level is how many contracts hold data and how many do
//    not; it cannot answer how many windows nobody ever asked for. That is why the bar below is
//    drawn in CONTRACTS and the chunk counts travel in the tooltip.

// ---------------------------------------------------------------------------
// The wire shape
// ---------------------------------------------------------------------------

/** `ExpiryCoverage` from api/schemas/catalog.py.
 *
 *  Deliberately not the `Coverage` in lib/api/types.ts. That type carries `chunks_missing`, which
 *  the backend does not send and cannot send: `candle_coverage` holds one row per fetched window,
 *  so it can count windows that errored and never windows nobody requested. Mirroring the real
 *  body here means a missing field is a compile error rather than an undefined rendered as NaN. */
export interface ExpiryCoverageWire {
  contracts_with_data: number
  contracts_without_data: number
  contracts_sealed: number
  chunks_ok: number
  chunks_empty: number
  chunks_error: number
  rows: number
}

/** `ExpiryOut` from api/schemas/catalog.py. */
export interface ExpiryRow {
  expiry_id: number
  expiry_date: string
  expiry_dow: number | null
  has_futures: boolean
  has_options: boolean
  futures_count: number | null
  options_count: number | null
  contract_count: number | null
  expiry_cycle: string | null
  expiry_cycle_source: string | null
  is_last_of_month: boolean | null
  discovered_at: string | null
  contracts_discovered_at: string | null
  contract_id_lo: number | null
  contract_id_hi: number | null
  min_strike: number | null
  max_strike: number | null
  strike_step: number | null
  coverage: ExpiryCoverageWire
}

export interface ExpiryPageWire {
  items: ExpiryRow[]
  next_cursor: string | null
}

/** The expiry date is the row identity everywhere: it is what the plan request carries, what the
 *  selection is keyed by, and what the cursor pages on. `expiry_id` is never sent to the plan. */
export function expiryRowId(row: ExpiryRow): string {
  return row.expiry_date
}

// ---------------------------------------------------------------------------
// Selection across pages
// ---------------------------------------------------------------------------

/** The selection the screen owns: expiry date to the row that was ticked.
 *
 *  The row travels with the key because the selection bar and the download sheet price what was
 *  selected, and a row from page one is not in the table's data once page two has loaded. */
export type ExpirySelection = Record<string, ExpiryRow>

/**
 * Folds one table selection change back into the screen's selection.
 *
 * The table's own state only describes the page it is holding. Select all on page two calls this
 * with a next state containing exactly page two's ids, and taking that literally would silently
 * drop everything ticked on page one. So keys for rows that are NOT on this page are carried
 * through untouched, and only the rows the user could actually see are added or removed.
 */
export function reconcileSelection(
  previous: ExpirySelection,
  pageRows: readonly ExpiryRow[],
  next: RowSelectionState,
): ExpirySelection {
  const onThisPage = new Map(pageRows.map((row) => [expiryRowId(row), row]))
  const merged: ExpirySelection = {}

  for (const [key, row] of Object.entries(previous)) {
    if (!onThisPage.has(key)) {
      merged[key] = row
    }
  }
  for (const [key, selected] of Object.entries(next)) {
    const row = onThisPage.get(key)
    if (selected && row) {
      merged[key] = row
    }
  }
  return merged
}

/** The table state for the page currently held. Keys for off-page selections are included so the
 *  header checkbox does not read as "none selected" while six rows are selected elsewhere. */
export function rowSelectionStateOf(selection: ExpirySelection): RowSelectionState {
  const state: RowSelectionState = {}
  for (const key of Object.keys(selection)) {
    state[key] = true
  }
  return state
}

/** Selected rows in expiry date order, which is the order the plan request wants them in. */
export function selectedRows(selection: ExpirySelection): ExpiryRow[] {
  return Object.values(selection).sort((left, right) =>
    left.expiry_date < right.expiry_date ? -1 : left.expiry_date > right.expiry_date ? 1 : 0,
  )
}

// ---------------------------------------------------------------------------
// Cell values
// ---------------------------------------------------------------------------

/**
 * The coverage bar for one expiry, in contracts.
 *
 * ok      contracts with at least one stored bar
 * empty   left at zero: the ledger counts empty WINDOWS, not contracts that are wholly empty,
 *         and inventing a contract figure from a chunk figure would be a number nobody wrote
 * missing contracts with nothing stored
 *
 * An expiry whose contracts have not been discovered has no contracts at all, so every segment is
 * zero and the bar renders its own "nothing discovered yet" state rather than a full bar.
 */
export function expiryCoverageBarProps(row: ExpiryRow): CoverageBarProps {
  const withData = row.coverage.contracts_with_data
  const without = row.coverage.contracts_without_data
  return {
    ok: withData,
    empty: 0,
    missing: without,
    rows: row.coverage.rows,
    label: 'Contracts holding data',
  }
}

/** Short IST weekday for an expiry date. A label, not a rule: nothing here decides whether a day
 *  is a trading day, because observed sessions run on Saturdays and Sundays too. */
const weekdayFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  weekday: 'short',
})

export function expiryWeekday(isoDate: string): string {
  const at = Date.parse(isoDate + 'T00:00:00+05:30')
  return Number.isNaN(at) ? '' : weekdayFormat.format(new Date(at))
}

/** "431 contracts, 3 fut and 428 opt", or what is known when the counts are absent. */
export function contractsText(row: ExpiryRow): string {
  if (row.contracts_discovered_at === null) {
    return 'not discovered'
  }
  const total = row.contract_count ?? 0
  const futures = row.futures_count ?? 0
  const options = row.options_count ?? 0
  return (
    formatInteger(total) +
    ' contracts, ' +
    formatInteger(futures) +
    ' fut and ' +
    formatInteger(options) +
    ' opt'
  )
}

/** The strike geometry as one line, so the sheet's strike scope can be judged before it is set. */
export function strikeRangeText(row: ExpiryRow): string {
  if (row.min_strike === null || row.max_strike === null) {
    return ''
  }
  const step = row.strike_step === null ? '' : ' step ' + formatStrike(row.strike_step)
  return formatStrike(row.min_strike) + ' to ' + formatStrike(row.max_strike) + step
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

export interface ExpirySelectTableProps {
  rows: ExpiryRow[]
  selection: ExpirySelection
  onSelectionChange: (selection: ExpirySelection) => void
  isLoading?: boolean
  emptyTitle?: string
  emptyDescription?: string
  emptyAction?: ReactNode
  footer?: ReactNode
  className?: string
}

export function ExpirySelectTable({
  rows,
  selection,
  onSelectionChange,
  isLoading = false,
  emptyTitle = 'No expiries yet',
  emptyDescription,
  emptyAction,
  footer,
  className,
}: ExpirySelectTableProps) {
  const rowSelection = useMemo(() => rowSelectionStateOf(selection), [selection])

  const handleSelectionChange: OnChangeFn<RowSelectionState> = (updater) => {
    const next = typeof updater === 'function' ? updater(rowSelection) : updater
    onSelectionChange(reconcileSelection(selection, rows, next))
  }

  const columns = useMemo<Array<ColumnDef<AppTableFeatures, ExpiryRow, unknown>>>(
    () => [
      {
        id: 'select',
        size: 36,
        header: ({ table }) => (
          <Checkbox
            aria-label="Select every expiry on this page"
            checked={
              table.getIsAllRowsSelected()
                ? true
                : table.getIsSomeRowsSelected()
                  ? 'indeterminate'
                  : false
            }
            onCheckedChange={(value) => {
              table.toggleAllRowsSelected(value === true)
            }}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            aria-label={'Select expiry ' + row.original.expiry_date}
            checked={row.getIsSelected()}
            onCheckedChange={(value) => {
              row.toggleSelected(value === true)
            }}
          />
        ),
      },
      {
        id: 'expiry_date',
        accessorKey: 'expiry_date',
        header: 'Expiry',
        cell: ({ row }) => (
          <div className="flex items-baseline gap-2">
            <span className="font-medium tabular-nums">{row.original.expiry_date}</span>
            <span className="text-xs text-muted-foreground">
              {expiryWeekday(row.original.expiry_date)}
            </span>
          </div>
        ),
      },
      {
        id: 'cycle',
        header: 'Cycle',
        cell: ({ row }) =>
          row.original.expiry_cycle ? (
            <span className="text-xs text-muted-foreground">
              {row.original.expiry_cycle}
              {row.original.expiry_cycle_source ? ' (' + row.original.expiry_cycle_source + ')' : ''}
            </span>
          ) : null,
      },
      {
        id: 'contracts',
        header: 'Contracts',
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span className="text-xs">{contractsText(row.original)}</span>
            {row.original.contracts_discovered_at === null ? (
              <Badge variant="outline" className="text-[0.65rem]">
                discovery needed
              </Badge>
            ) : null}
          </div>
        ),
      },
      {
        id: 'strikes',
        header: 'Strikes',
        meta: { numeric: true },
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">{strikeRangeText(row.original)}</span>
        ),
      },
      {
        id: 'coverage',
        header: 'Held',
        size: 180,
        cell: ({ row }) => {
          const props = expiryCoverageBarProps(row.original)
          const errors = row.original.coverage.chunks_error
          return (
            <div className="flex min-w-0 flex-col gap-1">
              <CoverageBar {...props} />
              <span className="text-[0.65rem] text-muted-foreground tabular-nums">
                {formatInteger(props.ok)} of {formatInteger(props.ok + props.missing)} contracts
                {errors > 0 ? ', ' + formatInteger(errors) + ' chunks errored' : ''}
              </span>
            </div>
          )
        },
      },
      {
        id: 'rows',
        header: 'Rows',
        meta: { numeric: true },
        cell: ({ row }) => formatCompact(row.original.coverage.rows),
      },
    ],
    [],
  )

  return (
    <DataTable<ExpiryRow>
      className={cn(className)}
      data={rows}
      columns={columns}
      getRowId={expiryRowId}
      enableRowSelection
      rowSelection={rowSelection}
      onRowSelectionChange={handleSelectionChange}
      isLoading={isLoading}
      emptyTitle={emptyTitle}
      emptyDescription={emptyDescription}
      emptyAction={emptyAction}
      footer={footer}
      maxHeight="calc(100vh - 21rem)"
    />
  )
}

export default ExpirySelectTable
