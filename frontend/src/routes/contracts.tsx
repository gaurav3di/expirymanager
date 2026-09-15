import { useCallback, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { DataTable } from '@/components/common/DataTable'
import type { DataTableProps } from '@/components/common/DataTable'
import { PageHeader } from '@/components/common/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api, isTerminalApiError } from '@/lib/api/client'
import type { QueryParams } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Paged, Underlying } from '@/lib/api/types'
import { formatCompact, formatDate, formatInteger, formatStrike } from '@/lib/format'
import { RESOLUTIONS, specForResId } from '@/lib/charts/intervals'

// The contract browser.
//
// Hundreds of thousands of rows, so every filter, every sort and every page boundary is the
// server's decision. The table is told manualSorting, a header click becomes a new request, and
// paging is by opaque cursor rather than by offset, because an offset into a set this size is a
// full scan per page and is wrong the moment a discovery job inserts a row behind the reader.
//
// A cursor cannot be walked backwards, so Previous is a stack of the cursors already used rather
// than an invented "cursor minus one". That is why the page index is explicit state here.

/** Matches the route's own default and maximum. */
const PAGE_SIZES = [50, 100, 200, 500] as const
const DEFAULT_LIMIT = 100

const SORT_COLUMNS = ['expiry_date', 'strike', 'fyers_symbol', 'rows'] as const
export type SortColumn = (typeof SORT_COLUMNS)[number]

const KIND_CHOICES = [
  { value: 'all', label: 'Futures and options' },
  { value: 'OPT', label: 'Options only' },
  { value: 'FUT', label: 'Futures only' },
] as const

const RIGHT_CHOICES = [
  { value: 'all', label: 'Both rights' },
  { value: 'CE', label: 'Calls' },
  { value: 'PE', label: 'Puts' },
] as const

/** Mirrors ContractOut.resolutions. Declared here rather than taken from lib/api/types because
 *  the route also returns chart_interval, which the hand written type predates. */
export interface ContractResolutionRow {
  res_id: number
  fyers_code: string | null
  chart_interval: string | null
  rows: number
  first_ts: string | null
  last_ts: string | null
}

export interface ContractRow {
  contract_id: number
  fyers_symbol: string
  underlying_id: number
  kind: string
  instrument_class: string | null
  expiry_date: string | null
  strike: number | null
  option_type: string | null
  lot_size: number | null
  sealed_at: string | null
  rows: number
  resolutions: ContractResolutionRow[]
}

export interface ContractFilters {
  underlyingId: number | null
  expiryFrom: string
  expiryTo: string
  kind: (typeof KIND_CHOICES)[number]['value']
  optionType: (typeof RIGHT_CHOICES)[number]['value']
  strikeMin: string
  strikeMax: string
  symbolContains: string
  /** Null means no opinion, true means only contracts holding rows. */
  hasData: boolean | null
  sealed: boolean | null
  resId: number | null
  sort: SortColumn
  dir: 'asc' | 'desc'
  limit: number
}

export const EMPTY_FILTERS: ContractFilters = {
  underlyingId: null,
  expiryFrom: '',
  expiryTo: '',
  kind: 'all',
  optionType: 'all',
  strikeMin: '',
  strikeMax: '',
  symbolContains: '',
  hasData: null,
  sealed: null,
  resId: null,
  sort: 'expiry_date',
  dir: 'asc',
  limit: DEFAULT_LIMIT,
}

function numberOrNull(value: string): number | null {
  if (value.trim() === '') {
    return null
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * Filters to wire parameters.
 *
 * Empty is omitted rather than sent as an empty string: the route validates option_type against
 * a pattern, and an empty string is a 422 rather than "no filter". Kept pure and exported so the
 * mapping is testable without a server, because a silently dropped filter shows up as a page of
 * rows that look plausible and are not what was asked for.
 */
export function contractQueryParams(
  filters: ContractFilters,
  cursor: string | null,
): QueryParams {
  const params: QueryParams = {
    sort: filters.sort,
    dir: filters.dir,
    limit: filters.limit,
  }
  if (filters.underlyingId !== null) {
    params.underlying_id = filters.underlyingId
  }
  if (filters.expiryFrom !== '') {
    params.expiry_from = filters.expiryFrom
  }
  if (filters.expiryTo !== '') {
    params.expiry_to = filters.expiryTo
  }
  if (filters.kind !== 'all') {
    params.kind = filters.kind
  }
  if (filters.optionType !== 'all') {
    params.option_type = filters.optionType
  }
  const strikeMin = numberOrNull(filters.strikeMin)
  if (strikeMin !== null) {
    params.strike_min = strikeMin
  }
  const strikeMax = numberOrNull(filters.strikeMax)
  if (strikeMax !== null) {
    params.strike_max = strikeMax
  }
  const contains = filters.symbolContains.trim()
  if (contains !== '') {
    params.symbol_contains = contains
  }
  if (filters.hasData !== null) {
    params.has_data = filters.hasData
  }
  if (filters.sealed !== null) {
    params.sealed = filters.sealed
  }
  if (filters.resId !== null) {
    params.res_id = filters.resId
  }
  if (cursor !== null) {
    params.cursor = cursor
  }
  return params
}

/** The resolutions a contract actually holds, shortest first, as chart interval labels. */
export function resolutionSummary(row: ContractRow): string[] {
  return [...row.resolutions]
    .filter((resolution) => resolution.rows > 0)
    .sort((left, right) => left.res_id - right.res_id)
    .map((resolution) => {
      const spec = specForResId(resolution.res_id)
      const label = resolution.chart_interval ?? spec?.chartInterval ?? String(resolution.res_id)
      return label + ' ' + formatCompact(resolution.rows)
    })
}

export function ContractsRoute() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const initialUnderlying = numberOrNull(searchParams.get('underlying_id') ?? '')
  const [filters, setFilters] = useState<ContractFilters>({
    ...EMPTY_FILTERS,
    underlyingId: initialUnderlying,
    expiryFrom: searchParams.get('expiry_from') ?? '',
    expiryTo: searchParams.get('expiry_to') ?? '',
  })
  /** The cursor for the page being shown, plus every cursor walked to get here. Index 0 is the
   *  first page, which has no cursor at all. */
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null])
  const [pageIndex, setPageIndex] = useState(0)

  const cursor = cursorStack[pageIndex] ?? null
  const params = useMemo(() => contractQueryParams(filters, cursor), [filters, cursor])

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({ active_only: false }),
    queryFn: () => api.get<Underlying[]>('/underlyings', { query: { active_only: false } }),
  })

  const contracts = useQuery({
    queryKey: queryKeys.contracts.list(params),
    queryFn: () => api.get<Paged<ContractRow>>('/contracts', { query: params }),
    retry: (attempt, error) => !isTerminalApiError(error) && attempt < 2,
  })

  /** Any filter change invalidates every cursor already collected: they were minted against the
   *  old ordering and the route answers 400 invalid_cursor for one that does not match. */
  const applyFilters = useCallback((patch: Partial<ContractFilters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setCursorStack([null])
    setPageIndex(0)
  }, [])

  const nextCursor = contracts.data?.next_cursor ?? null
  const rows = useMemo(() => contracts.data?.items ?? [], [contracts.data])

  const goNext = useCallback(() => {
    if (nextCursor === null) {
      return
    }
    setCursorStack((stack) => {
      const next = stack.slice(0, pageIndex + 1)
      next.push(nextCursor)
      return next
    })
    setPageIndex((index) => index + 1)
  }, [nextCursor, pageIndex])

  const goPrevious = useCallback(() => {
    setPageIndex((index) => Math.max(0, index - 1))
  }, [])

  const openChart = useCallback(
    (row: ContractRow) => {
      void navigate('/chart?contract_id=' + String(row.contract_id))
    },
    [navigate],
  )

  /** The exact column type the shared table takes, including its deliberately open value
   *  parameter, so a heterogeneous column list needs no cast at the call site. */
  const columns = useMemo<DataTableProps<ContractRow>['columns']>(
    () => [
      {
        id: 'fyers_symbol',
        header: 'Symbol',
        accessorFn: (row) => row.fyers_symbol,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.fyers_symbol}</span>,
      },
      {
        id: 'kind',
        header: 'Kind',
        enableSorting: false,
        accessorFn: (row) => row.kind,
        cell: ({ row }) => (
          <span className="text-xs">
            {row.original.kind}
            {row.original.option_type ? ' ' + row.original.option_type : ''}
          </span>
        ),
      },
      {
        id: 'expiry_date',
        header: 'Expiry',
        accessorFn: (row) => row.expiry_date,
        cell: ({ row }) => formatDate(row.original.expiry_date),
      },
      {
        id: 'strike',
        header: 'Strike',
        accessorFn: (row) => row.strike,
        meta: { numeric: true },
        cell: ({ row }) => (row.original.strike === null ? '' : formatStrike(row.original.strike)),
      },
      {
        id: 'lot_size',
        header: 'Lot',
        enableSorting: false,
        accessorFn: (row) => row.lot_size,
        meta: { numeric: true },
        cell: ({ row }) =>
          row.original.lot_size === null ? '' : formatInteger(row.original.lot_size),
      },
      {
        id: 'rows',
        header: 'Bars',
        accessorFn: (row) => row.rows,
        meta: { numeric: true },
        cell: ({ row }) => formatInteger(row.original.rows),
      },
      {
        id: 'resolutions',
        header: 'Resolutions',
        enableSorting: false,
        accessorFn: (row) => row.resolutions.length,
        cell: ({ row }) => {
          const held = resolutionSummary(row.original)
          if (held.length === 0) {
            return <span className="text-xs text-muted-foreground">none downloaded</span>
          }
          return (
            <div className="flex flex-wrap gap-1">
              {held.map((label) => (
                <Badge key={label} variant="outline" className="font-mono text-[0.65rem]">
                  {label}
                </Badge>
              ))}
            </div>
          )
        },
      },
      {
        id: 'sealed_at',
        header: 'Sealed',
        enableSorting: false,
        accessorFn: (row) => row.sealed_at,
        cell: ({ row }) =>
          row.original.sealed_at === null ? (
            <span className="text-xs text-muted-foreground">open</span>
          ) : (
            <span className="text-xs">sealed</span>
          ),
      },
    ],
    [],
  )

  const sorting = useMemo(
    () => [{ id: filters.sort, desc: filters.dir === 'desc' }],
    [filters.sort, filters.dir],
  )

  const underlyingOptions = underlyings.data ?? []
  const activeUnderlying = underlyingOptions.find(
    (item) => item.underlying_id === filters.underlyingId,
  )

  return (
    <div className="flex min-h-0 flex-col">
      <PageHeader
        title="Contracts"
        description={
          'Every contract the catalogue knows, filtered and paged by the server. ' +
          'Open one to chart it.'
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Underlying</Label>
            <Select
              value={filters.underlyingId === null ? 'all' : String(filters.underlyingId)}
              onValueChange={(value) =>
                applyFilters({ underlyingId: value === 'all' ? null : Number(value) })
              }
            >
              <SelectTrigger size="sm" className="w-48">
                <SelectValue placeholder="Every underlying" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Every underlying</SelectItem>
                {underlyingOptions.map((item) => (
                  <SelectItem key={item.underlying_id} value={String(item.underlying_id)}>
                    {item.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground" htmlFor="contract-symbol">
              Symbol contains
            </Label>
            <Input
              id="contract-symbol"
              className="h-7 w-48 font-mono text-xs"
              value={filters.symbolContains}
              placeholder="23000CE"
              onChange={(event) => applyFilters({ symbolContains: event.target.value })}
            />
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground" htmlFor="contract-expiry-from">
              Expiry from
            </Label>
            <Input
              id="contract-expiry-from"
              type="date"
              className="h-7 w-36 text-xs"
              value={filters.expiryFrom}
              onChange={(event) => applyFilters({ expiryFrom: event.target.value })}
            />
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground" htmlFor="contract-expiry-to">
              Expiry to
            </Label>
            <Input
              id="contract-expiry-to"
              type="date"
              className="h-7 w-36 text-xs"
              value={filters.expiryTo}
              onChange={(event) => applyFilters({ expiryTo: event.target.value })}
            />
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Kind</Label>
            <Select
              value={filters.kind}
              onValueChange={(value) =>
                applyFilters({ kind: value as ContractFilters['kind'] })
              }
            >
              <SelectTrigger size="sm" className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {KIND_CHOICES.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    {choice.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Right</Label>
            <Select
              value={filters.optionType}
              onValueChange={(value) =>
                applyFilters({ optionType: value as ContractFilters['optionType'] })
              }
            >
              <SelectTrigger size="sm" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RIGHT_CHOICES.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    {choice.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground" htmlFor="contract-strike-min">
              Strike range
            </Label>
            <div className="flex items-center gap-1">
              <Input
                id="contract-strike-min"
                inputMode="decimal"
                className="h-7 w-24 text-xs"
                placeholder="min"
                value={filters.strikeMin}
                onChange={(event) => applyFilters({ strikeMin: event.target.value })}
              />
              <Input
                inputMode="decimal"
                className="h-7 w-24 text-xs"
                placeholder="max"
                aria-label="Maximum strike"
                value={filters.strikeMax}
                onChange={(event) => applyFilters({ strikeMax: event.target.value })}
              />
            </div>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Resolution</Label>
            <Select
              value={filters.resId === null ? 'any' : String(filters.resId)}
              onValueChange={(value) =>
                applyFilters({ resId: value === 'any' ? null : Number(value) })
              }
            >
              <SelectTrigger size="sm" className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="any">Any resolution</SelectItem>
                {RESOLUTIONS.map((spec) => (
                  <SelectItem key={spec.resId} value={String(spec.resId)}>
                    {spec.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <label className="flex items-center gap-2 pb-1 text-xs">
            <Checkbox
              checked={filters.hasData === true}
              onCheckedChange={(checked) =>
                applyFilters({ hasData: checked === true ? true : null })
              }
            />
            Only contracts with bars
          </label>

          <label className="flex items-center gap-2 pb-1 text-xs">
            <Checkbox
              checked={filters.sealed === true}
              onCheckedChange={(checked) =>
                applyFilters({ sealed: checked === true ? true : null })
              }
            />
            Only sealed
          </label>

          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setFilters(EMPTY_FILTERS)
              setCursorStack([null])
              setPageIndex(0)
            }}
          >
            Clear filters
          </Button>
        </div>
      </PageHeader>

      <div className="min-h-0 flex-1 p-5">
        {contracts.isError ? (
          <p className="mb-3 text-sm text-destructive">{(contracts.error as Error).message}</p>
        ) : null}

        <DataTable<ContractRow>
          data={rows}
          columns={columns}
          getRowId={(row) => String(row.contract_id)}
          sorting={sorting}
          onSortingChange={(updater) => {
            const next = typeof updater === 'function' ? updater(sorting) : updater
            const first = next[0]
            if (first === undefined) {
              return
            }
            if (!SORT_COLUMNS.includes(first.id as SortColumn)) {
              return
            }
            applyFilters({ sort: first.id as SortColumn, dir: first.desc ? 'desc' : 'asc' })
          }}
          isLoading={contracts.isPending}
          loadingRows={12}
          onRowClick={openChart}
          maxHeight="calc(100vh - 20rem)"
          emptyTitle="No contracts match"
          emptyDescription={
            activeUnderlying === undefined
              ? 'Pick an underlying, or widen the filters. Contracts appear once an expiry discovery job has run.'
              : 'Nothing under ' +
                activeUnderlying.display_name +
                ' matches these filters. Expiry discovery fills this table.'
          }
          footer={
            <>
              <span>
                {contracts.isPending
                  ? 'Loading'
                  : 'Page ' +
                    String(pageIndex + 1) +
                    ', ' +
                    formatInteger(rows.length) +
                    ' contracts shown'}
              </span>
              <span className="flex items-center gap-2">
                <Select
                  value={String(filters.limit)}
                  onValueChange={(value) => applyFilters({ limit: Number(value) })}
                >
                  <SelectTrigger size="sm" className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PAGE_SIZES.map((size) => (
                      <SelectItem key={size} value={String(size)}>
                        {String(size)} per page
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={pageIndex === 0 || contracts.isPending}
                  onClick={goPrevious}
                >
                  Previous
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={nextCursor === null || contracts.isPending}
                  onClick={goNext}
                >
                  Next
                </Button>
              </span>
            </>
          }
        />
      </div>
    </div>
  )
}

export default ContractsRoute
