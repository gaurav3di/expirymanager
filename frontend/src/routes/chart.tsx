import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTheme } from 'next-themes'
import { cn } from 'cn'

import { ExpiryChart } from '@/components/charts/ExpiryChart'
import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Paged, Underlying } from '@/lib/api/types'
import { expiryFeed } from '@/lib/charts/expiryFeed'
import {
  defaultInterval,
  intervalPills,
  resolveInterval,
  selectableIntervals,
} from '@/lib/charts/intervals'
import type { IntervalPill } from '@/lib/charts/intervals'
import { formatCompact, formatDateTime, formatInteger } from '@/lib/format'
import type { ContractRow } from '@/routes/contracts'

// The chart workbench.
//
// Three things this screen owns, and the chart component owns nothing else:
//
//  1. Which contract. Picked from the catalogue by search, or arrived at by ?contract_id= from
//     the contract browser and the option chain.
//  2. Which interval. The pills come from the contract's own bounds, so a resolution with no
//     bars behind it is rendered disabled with the reason on it rather than offered and then
//     answering empty.
//  3. Priming the feed. The bounds query this screen already makes for the pills is pushed into
//     the feed before the chart mounts, which is what stops the first load spending a round trip
//     on a forty two hour window ending today against a contract that expired in March.

/** UTC seconds, as the bounds route emits them. */
export interface BoundsResolution {
  res_id: number
  fyers_code: string | null
  chart_interval: string | null
  first_ts: number | null
  last_ts: number | null
  rows: number
}

export interface ContractBoundsPayload {
  contract_id: number
  fyers_symbol: string
  resolutions: BoundsResolution[]
}

const SEARCH_LIMIT = 25

/** The exchange prefix Fyers puts on the symbol itself. The widget is handed both, and the
 *  prefix is what the top bar shows next to the name. */
export function exchangeOf(symbol: string): string {
  const head = symbol.split(':')[0]
  return head === undefined || head === symbol ? '' : head
}

/** IST wall clock text from UTC seconds, for the bounds readout. The bounds route emits seconds
 *  rather than the ISO text the rest of the catalogue uses, so this is the one conversion. */
export function istTextFromEpoch(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) {
    return null
  }
  return new Date(seconds * 1000).toISOString().replace('T', ' ').slice(0, 19) + '+00:00'
}

export function ChartRoute() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { resolvedTheme } = useTheme()
  const theme: 'dark' | 'light' = resolvedTheme === 'light' ? 'light' : 'dark'

  const requestedId = Number(searchParams.get('contract_id') ?? '')
  const contractId = Number.isFinite(requestedId) && requestedId > 0 ? requestedId : null

  const [search, setSearch] = useState('')
  const [underlyingId, setUnderlyingId] = useState<number | null>(null)
  const [chartInterval, setChartInterval] = useState<string | null>(null)
  const [showOpenInterest, setShowOpenInterest] = useState(false)
  const [status, setStatus] = useState<{ bars: number; error?: string } | null>(null)

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({ active_only: false }),
    queryFn: () => api.get<Underlying[]>('/underlyings', { query: { active_only: false } }),
  })

  const searchParamsForList = useMemo(() => {
    const query: Record<string, string | number | boolean> = {
      has_data: true,
      limit: SEARCH_LIMIT,
      sort: 'expiry_date',
      dir: 'desc',
    }
    if (search.trim() !== '') {
      query.symbol_contains = search.trim()
    }
    if (underlyingId !== null) {
      query.underlying_id = underlyingId
    }
    return query
  }, [search, underlyingId])

  const matches = useQuery({
    queryKey: queryKeys.contracts.list(searchParamsForList),
    queryFn: () => api.get<Paged<ContractRow>>('/contracts', { query: searchParamsForList }),
  })

  const contract = useQuery({
    queryKey: queryKeys.contracts.detail(contractId ?? 0),
    queryFn: () => api.get<ContractRow>('/contracts/' + String(contractId)),
    enabled: contractId !== null,
  })

  const bounds = useQuery({
    queryKey: queryKeys.contracts.bounds(contractId ?? 0),
    queryFn: () => api.get<ContractBoundsPayload>('/contracts/' + String(contractId) + '/bounds'),
    enabled: contractId !== null,
  })

  const symbol = contract.data?.fyers_symbol ?? bounds.data?.fyers_symbol ?? null

  const declaredResolutions = useMemo(() => {
    const owner = (underlyings.data ?? []).find(
      (item) => item.underlying_id === contract.data?.underlying_id,
    )
    return owner?.default_resolutions ?? []
  }, [underlyings.data, contract.data])

  const pills: IntervalPill[] = useMemo(
    () => intervalPills(declaredResolutions, bounds.data?.resolutions ?? []),
    [declaredResolutions, bounds.data],
  )

  const selectable = useMemo(() => selectableIntervals(pills), [pills])

  // Prime the feed from the bounds this screen already holds, and tell it which contract the
  // symbol is so every request goes by id. Both are pushed before the chart is allowed to mount,
  // which is what the mount gate below is for.
  const [primedFor, setPrimedFor] = useState<string | null>(null)
  useEffect(() => {
    if (symbol === null || contractId === null || bounds.data === undefined) {
      return
    }
    expiryFeed.registerContract(symbol, contractId)
    for (const resolution of bounds.data.resolutions) {
      const code = resolution.chart_interval
      if (code === null || resolution.first_ts === null || resolution.last_ts === null) {
        continue
      }
      expiryFeed.primeBounds(symbol, code, {
        firstTs: resolution.first_ts,
        lastTs: resolution.last_ts,
      })
    }
    setPrimedFor(symbol)
  }, [symbol, contractId, bounds.data])

  // The interval follows the contract: a remembered code is kept only while the new contract can
  // actually draw it, otherwise the finest one with rows wins.
  useEffect(() => {
    if (pills.length === 0) {
      return
    }
    setChartInterval((current) => resolveInterval(pills, current) ?? defaultInterval(pills))
  }, [pills])

  const selectContract = useCallback(
    (row: ContractRow) => {
      setSearchParams({ contract_id: String(row.contract_id) })
      setStatus(null)
    },
    [setSearchParams],
  )

  const onData = useCallback((bars: number, error?: string) => {
    setStatus({ bars, error })
  }, [])

  const activePill = pills.find((pill) => pill.interval === chartInterval) ?? null

  // The chart is only handed a symbol once the feed has been primed with that contract's bounds,
  // so the very first load is already clamped onto the contract's own life rather than onto a
  // window ending today. Held in state rather than derived, so the terminal keeps drawing the
  // previous contract while the next one's bounds are still in flight instead of unmounting.
  const [chartSymbol, setChartSymbol] = useState<string | null>(null)
  useEffect(() => {
    if (symbol !== null && primedFor === symbol && selectable.length > 0) {
      setChartSymbol(symbol)
    }
  }, [symbol, primedFor, selectable.length])

  const switching = symbol !== null && chartSymbol !== null && symbol !== chartSymbol
  const nothingHeld =
    contractId !== null && !bounds.isPending && bounds.data !== undefined && selectable.length === 0

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Chart"
        description={
          chartSymbol === null
            ? 'Pick an expired contract to chart it.'
            : chartSymbol + ' drawn from the candles this app downloaded.'
        }
        actions={
          chartSymbol === null ? null : (
            <label className="flex items-center gap-2 text-xs">
              <Switch
                checked={showOpenInterest}
                onCheckedChange={(checked) => setShowOpenInterest(checked === true)}
                aria-label="Open interest pane"
              />
              Open interest pane
            </label>
          )
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Underlying</Label>
            <Select
              value={underlyingId === null ? 'all' : String(underlyingId)}
              onValueChange={(value) => setUnderlyingId(value === 'all' ? null : Number(value))}
            >
              <SelectTrigger size="sm" className="w-44">
                <SelectValue placeholder="Every underlying" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Every underlying</SelectItem>
                {(underlyings.data ?? []).map((item) => (
                  <SelectItem key={item.underlying_id} value={String(item.underlying_id)}>
                    {item.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground" htmlFor="chart-search">
              Contract
            </Label>
            <Input
              id="chart-search"
              className="h-7 w-64 font-mono text-xs"
              placeholder="NIFTY25MAR"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>

          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <Label className="text-xs text-muted-foreground">
              {matches.isPending
                ? 'Searching'
                : formatInteger(matches.data?.items.length ?? 0) + ' contracts with bars'}
            </Label>
            <div className="flex min-w-0 flex-wrap gap-1">
              {(matches.data?.items ?? []).slice(0, SEARCH_LIMIT).map((row) => (
                <Button
                  key={row.contract_id}
                  size="xs"
                  variant={row.contract_id === contractId ? 'default' : 'outline'}
                  className="font-mono text-[0.65rem]"
                  onClick={() => selectContract(row)}
                >
                  {row.fyers_symbol}
                </Button>
              ))}
            </div>
          </div>
        </div>

        {pills.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[0.65rem] tracking-wide text-muted-foreground uppercase">
              Interval
            </span>
            {pills.map((pill) => (
              <Button
                key={pill.interval}
                size="xs"
                variant={pill.interval === chartInterval ? 'default' : 'outline'}
                disabled={pill.state !== 'available'}
                // A pill with nothing behind it is rendered disabled with its reason attached
                // rather than hidden, so a missing download does not read as a missing feature.
                title={pill.reason ?? formatInteger(pill.rows) + ' bars'}
                onClick={() => setChartInterval(pill.interval)}
                className={cn(pill.state !== 'available' && 'opacity-60')}
              >
                {pill.interval}
                <span className="ml-1 text-[0.6rem] opacity-70">
                  {pill.state === 'available' ? formatCompact(pill.rows) : pill.reason}
                </span>
              </Button>
            ))}
          </div>
        ) : null}
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-2 p-5">
        {bounds.isError ? (
          <p className="text-sm text-destructive">{(bounds.error as Error).message}</p>
        ) : null}

        {nothingHeld ? (
          <p className="text-sm text-muted-foreground">
            This contract holds no candles at any resolution yet, so there is nothing to draw.
            Download it from the expiries screen first.
          </p>
        ) : null}

        {chartSymbol === null || chartInterval === null ? (
          <EmptyState
            title={contractId === null ? 'No contract selected' : 'Loading the contract'}
            description={
              contractId === null
                ? 'Search above, or open one from the contract browser or the option chain.'
                : 'Reading the bounds the chart window is clamped onto.'
            }
          />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
              <Badge variant="outline" className="font-mono text-[0.65rem]">
                {chartSymbol}
              </Badge>
              {activePill === null ? null : (
                <span>
                  {formatInteger(activePill.rows) +
                    ' bars held at ' +
                    activePill.interval +
                    ', ' +
                    formatDateTime(istTextFromEpoch(activePill.firstTs)) +
                    ' to ' +
                    formatDateTime(istTextFromEpoch(activePill.lastTs))}
                </span>
              )}
              {switching ? <span>loading {symbol}</span> : null}
              {status === null ? null : status.error !== undefined ? (
                <span className="text-destructive">{status.error}</span>
              ) : (
                <span>{formatInteger(status.bars) + ' bars drawn'}</span>
              )}
            </div>

            <div className="min-h-0 flex-1">
              {/*
                Keyed on the pill list, and on nothing else.

                The widget fixes its own interval pills at construction, so a contract whose
                downloaded resolutions differ genuinely needs a new terminal or its pills would
                offer an interval that answers empty. A contract with the same resolutions keeps
                the running instance and changes through setSymbol, which is what preserves the
                user's indicators, drawings and pane layout. The symbol and the interval are
                deliberately absent from this key.
              */}
              <ExpiryChart
                key={selectable.join(',')}
                symbol={chartSymbol}
                exchange={exchangeOf(chartSymbol)}
                interval={chartInterval}
                intervals={selectable}
                theme={theme}
                showOpenInterest={showOpenInterest}
                onData={onData}
                onIntervalChange={setChartInterval}
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export default ChartRoute
