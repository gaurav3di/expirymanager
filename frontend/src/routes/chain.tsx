import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { ChainGrid } from '@/components/chain/ChainGrid'
import type { ChainGridRow, ChainLegValues } from '@/components/chain/ChainGrid'
import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
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
import { api } from '@/lib/api/client'
import type { QueryParams } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Paged, Underlying } from '@/lib/api/types'
import { mapCandles } from '@/lib/charts/expiryFeed'
import type { BarsPayload } from '@/lib/charts/expiryFeed'
import { intervalPills, selectableIntervals, fyersCodeForInterval } from '@/lib/charts/intervals'
import { formatDate, formatInteger, formatStrike } from '@/lib/format'
import type { ContractRow } from '@/routes/contracts'

// The option chain at one instant, with a scrubber over the trading day.
//
// THE SCRUBBER'S BOUNDS ARE DATA, NEVER A CONSTANT. Nothing here knows a market open, a market
// close or a session length, because none of those are constants in this dataset: the NSE
// derivatives close moved from 15:30 to 15:40 on 2026-08-03, and special sessions have run on a
// Saturday and on a Sunday. So every stop on the scrubber is a bar this app actually downloaded:
// the timeline is the timestamps of one contract in the expiry for the selected day, and the
// session open and close printed beside it are the first and last of those timestamps.
//
// Day navigation works the same way. There is no "previous weekday" arithmetic; the previous
// trading day is found by asking the bars route for the one bar before this day started, and the
// next by asking for the first bar after it ended. A holiday, a weekend and a special Sunday
// session are all handled by the same two requests, because all three are questions about what
// was traded rather than about what the calendar says.

const IST_OFFSET_SECONDS = 19_800
const DAY_SECONDS = 86_400

/** How far forward to look for the next day holding bars. One bounded request rather than a loop
 *  of day probes. A gap wider than this is a hole in the download rather than a closed exchange,
 *  and the coverage screen is where that is found, not this button. */
const FORWARD_SEARCH_DAYS = 31

export interface ChainLegPayload {
  contract_id: number
  fyers_symbol: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  oi: number | null
}

export interface ChainRowPayload {
  strike: number
  lot_size: number | null
  is_atm: boolean
  ce: ChainLegPayload | null
  pe: ChainLegPayload | null
}

export interface ChainPayload {
  underlying_id: number
  expiry_date: string
  resolution: string
  res_id: number
  ts: number | null
  requested_ts: number | null
  spot: number | null
  atm_strike: number | null
  rows: ChainRowPayload[]
}

export interface ExpiryRow {
  expiry_id: number
  expiry_date: string
  contract_count: number | null
  options_count: number | null
  contract_id_lo: number | null
  contract_id_hi: number | null
}

// ---------------------------------------------------------------------------
// Pure time helpers. IST is a fixed +05:30 offset with no daylight saving, which is why these are
// arithmetic rather than a formatter round trip. They convert BETWEEN representations; they never
// decide when a session starts.
// ---------------------------------------------------------------------------

/** The IST calendar date an instant falls on, as YYYY-MM-DD. */
export function istDateOf(epochSeconds: number): string {
  return new Date((epochSeconds + IST_OFFSET_SECONDS) * 1000).toISOString().slice(0, 10)
}

/** UTC seconds at IST midnight opening the given date. Not a session open: the session open is
 *  whatever the first bar of the day turns out to be. */
export function istMidnight(date: string): number {
  return Math.floor(Date.parse(date + 'T00:00:00Z') / 1000) - IST_OFFSET_SECONDS
}

/** The IST wall clock of an instant, HH:MM:SS. */
export function istClock(epochSeconds: number): string {
  return new Date((epochSeconds + IST_OFFSET_SECONDS) * 1000).toISOString().slice(11, 19)
}

/** The window covering one IST date, inclusive of both edges. */
export function istDayWindow(date: string): { from: number; to: number } {
  const from = istMidnight(date)
  return { from, to: from + DAY_SECONDS - 1 }
}

/**
 * The index of the scrubber stop nearest an instant.
 *
 * Nearest at or before, so dragging never shows a chain from the future of the pointer. Returns
 * the last index when the instant is past the end of the day, and 0 for an empty timeline.
 */
export function nearestIndex(timeline: readonly number[], moment: number): number {
  if (timeline.length === 0) {
    return 0
  }
  let index = 0
  for (let position = 0; position < timeline.length; position += 1) {
    if (timeline[position] <= moment) {
      index = position
    } else {
      break
    }
  }
  return index
}

/** The session the data shows for this day: its first bar, its last bar and how many there were.
 *  Every number comes from the timeline, so a 15:40 close and a Sunday session both report
 *  themselves without anything here being told they exist. */
export function sessionSummary(timeline: readonly number[]): string {
  if (timeline.length === 0) {
    return 'no bars on this day'
  }
  const open = istClock(timeline[0])
  const close = istClock(timeline[timeline.length - 1])
  return (
    'observed session ' +
    open +
    ' to ' +
    close +
    ' IST, ' +
    formatInteger(timeline.length) +
    ' bars'
  )
}

function toGridRow(row: ChainRowPayload): ChainGridRow {
  return {
    strike: row.strike,
    lot_size: row.lot_size,
    is_atm: row.is_atm,
    ce: row.ce,
    pe: row.pe,
  }
}

export function ChainRoute() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [underlyingId, setUnderlyingId] = useState<number | null>(() => {
    const raw = Number(searchParams.get('underlying_id') ?? '')
    return Number.isFinite(raw) && raw > 0 ? raw : null
  })
  const [expiryDate, setExpiryDate] = useState<string | null>(
    () => searchParams.get('expiry_date'),
  )
  const [interval, setChainInterval] = useState<string | null>(null)
  /** The instant the grid is showing. Null means "the last bar the expiry holds", which is what
   *  the route answers when no timestamp is sent. */
  const [ts, setTs] = useState<number | null>(null)

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({ active_only: false }),
    queryFn: () => api.get<Underlying[]>('/underlyings', { query: { active_only: false } }),
  })

  const expiryQuery = useMemo(
    () => ({ limit: 200 }),
    [],
  )
  const expiries = useQuery({
    queryKey: queryKeys.underlyings.expiries(underlyingId ?? 0, expiryQuery),
    queryFn: () =>
      api.get<Paged<ExpiryRow>>('/underlyings/' + String(underlyingId) + '/expiries', {
        query: expiryQuery,
      }),
    enabled: underlyingId !== null,
  })

  // The resolutions this expiry actually holds, read off its own contracts. The chain route takes
  // a resolution, and offering one that was never downloaded answers an empty grid.
  // No has_data filter: the expiry contracts route documents no such parameter, and FastAPI
  // ignores an unknown one silently, which would read as a filter that worked. The rows that hold
  // bars are picked out below instead.
  const contractsQuery = useMemo(() => ({ limit: 200 }), [])
  const expiryContracts = useQuery({
    queryKey: queryKeys.expiries.contracts(underlyingId ?? 0, expiryDate ?? '', contractsQuery),
    queryFn: () =>
      api.get<Paged<ContractRow>>(
        '/expiries/' + String(underlyingId) + '/' + String(expiryDate) + '/contracts',
        { query: contractsQuery },
      ),
    enabled: underlyingId !== null && expiryDate !== null,
  })

  const pills = useMemo(() => {
    const seen = new Map<number, { res_id: number; chart_interval: string | null; rows: number }>()
    for (const contract of expiryContracts.data?.items ?? []) {
      for (const resolution of contract.resolutions) {
        const existing = seen.get(resolution.res_id)
        seen.set(resolution.res_id, {
          res_id: resolution.res_id,
          chart_interval: resolution.chart_interval,
          rows: (existing?.rows ?? 0) + resolution.rows,
        })
      }
    }
    return intervalPills([], [...seen.values()])
  }, [expiryContracts.data])

  const selectable = useMemo(() => selectableIntervals(pills), [pills])

  useEffect(() => {
    if (selectable.length === 0) {
      return
    }
    setChainInterval((current) =>
      current !== null && selectable.includes(current) ? current : selectable[0],
    )
  }, [selectable])

  const chainParams = useMemo<QueryParams | null>(() => {
    if (underlyingId === null || expiryDate === null || interval === null) {
      return null
    }
    const resolution = fyersCodeForInterval(interval)
    if (resolution === null) {
      return null
    }
    const params: QueryParams = {
      underlying_id: underlyingId,
      expiry_date: expiryDate,
      resolution,
    }
    if (ts !== null) {
      params.ts = ts
    }
    return params
  }, [underlyingId, expiryDate, interval, ts])

  const chain = useQuery({
    queryKey: queryKeys.chain.slice(chainParams ?? undefined),
    queryFn: () => api.get<ChainPayload>('/chain', { query: chainParams ?? {} }),
    enabled: chainParams !== null,
  })

  /** The instant the server actually served, which is the requested one snapped back to a bar. */
  const servedTs = chain.data?.ts ?? null
  const day = servedTs === null ? null : istDateOf(servedTs)

  /**
   * The timeline the scrubber steps over: the bars of one contract in this expiry, for the day
   * being shown. One contract is enough, and the busiest one is the right one: a thin strike
   * would miss stops the chain has rows at.
   */
  const timelineContract = useMemo(() => {
    let best: ContractRow | null = null
    for (const contract of expiryContracts.data?.items ?? []) {
      if (contract.rows <= 0) {
        continue
      }
      if (best === null || contract.rows > best.rows) {
        best = contract
      }
    }
    return best
  }, [expiryContracts.data])

  const timelineParams = useMemo<QueryParams | null>(() => {
    if (timelineContract === null || day === null || interval === null) {
      return null
    }
    const resolution = fyersCodeForInterval(interval)
    if (resolution === null) {
      return null
    }
    const window = istDayWindow(day)
    return {
      contract_id: timelineContract.contract_id,
      resolution,
      from: window.from,
      to: window.to,
      include_oi: false,
    }
  }, [timelineContract, day, interval])

  const timelineQuery = useQuery({
    queryKey: queryKeys.bars.range(timelineParams ?? undefined),
    queryFn: () => api.get<BarsPayload>('/bars', { query: timelineParams ?? {} }),
    enabled: timelineParams !== null,
  })

  const timeline = useMemo(() => {
    const payload = timelineQuery.data
    if (payload === undefined) {
      return [] as number[]
    }
    // Only the bars that really fall on the day asked for. The bars route slides a window that
    // runs past the contract's last bar, so a day after the contract expired would otherwise come
    // back carrying the final session's bars under today's date.
    const wanted = day === null ? null : istDayWindow(day)
    return mapCandles(payload.columns ?? [], payload.candles ?? [])
      .map((bar) => bar.time)
      .filter((time) => wanted === null || (time >= wanted.from && time <= wanted.to))
  }, [timelineQuery.data, day])

  const scrubIndex = useMemo(
    () => (servedTs === null ? 0 : nearestIndex(timeline, servedTs)),
    [timeline, servedTs],
  )

  const stepDay = useCallback(
    async (direction: -1 | 1) => {
      if (day === null || timelineContract === null || interval === null) {
        return
      }
      const resolution = fyersCodeForInterval(interval)
      if (resolution === null) {
        return
      }
      const window = istDayWindow(day)
      if (direction === -1) {
        // One bar before this day began. Whatever day it lands on is the previous trading day,
        // holiday and weekend rules included, because it is the previous bar that exists.
        const payload = await api.get<BarsPayload>('/bars/before', {
          query: {
            contract_id: timelineContract.contract_id,
            resolution,
            before: window.from,
            count: 1,
            include_oi: false,
          },
        })
        const bars = mapCandles(payload.columns ?? [], payload.candles ?? [])
        const last = bars[bars.length - 1]
        if (last !== undefined) {
          setTs(last.time)
        }
        return
      }
      const payload = await api.get<BarsPayload>('/bars', {
        query: {
          contract_id: timelineContract.contract_id,
          resolution,
          from: window.to + 1,
          to: window.to + FORWARD_SEARCH_DAYS * DAY_SECONDS,
          include_oi: false,
        },
      })
      const bars = mapCandles(payload.columns ?? [], payload.candles ?? [])
      const first = bars.find((bar) => bar.time > window.to)
      if (first !== undefined) {
        setTs(first.time)
      }
    },
    [day, timelineContract, interval],
  )

  const openLeg = useCallback(
    (leg: ChainLegValues) => {
      void navigate('/chart?contract_id=' + String(leg.contract_id))
    },
    [navigate],
  )

  const selectUnderlying = useCallback(
    (value: string) => {
      const next = value === 'none' ? null : Number(value)
      setUnderlyingId(next)
      setExpiryDate(null)
      setTs(null)
      setSearchParams(next === null ? {} : { underlying_id: String(next) })
    },
    [setSearchParams],
  )

  const selectExpiry = useCallback(
    (value: string) => {
      setExpiryDate(value)
      setTs(null)
      setSearchParams(
        underlyingId === null
          ? { expiry_date: value }
          : { underlying_id: String(underlyingId), expiry_date: value },
      )
    },
    [setSearchParams, underlyingId],
  )

  const rows = useMemo(
    () => (chain.data?.rows ?? []).map(toGridRow),
    [chain.data],
  )
  const atmRow = rows.find((row) => row.is_atm) ?? null

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Option chain"
        description={
          expiryDate === null
            ? 'Pick an underlying and an expiry to read its chain at any instant it traded.'
            : 'The chain as it stood at one bar, from the candles this app downloaded.'
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Underlying</Label>
            <Select
              value={underlyingId === null ? 'none' : String(underlyingId)}
              onValueChange={selectUnderlying}
            >
              <SelectTrigger size="sm" className="w-44">
                <SelectValue placeholder="Choose one" />
              </SelectTrigger>
              <SelectContent>
                {(underlyings.data ?? []).map((item) => (
                  <SelectItem key={item.underlying_id} value={String(item.underlying_id)}>
                    {item.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Expiry</Label>
            <Select
              value={expiryDate ?? ''}
              onValueChange={selectExpiry}
              disabled={underlyingId === null || expiries.isPending}
            >
              <SelectTrigger size="sm" className="w-44">
                <SelectValue placeholder="Choose one" />
              </SelectTrigger>
              <SelectContent>
                {(expiries.data?.items ?? []).map((row) => (
                  <SelectItem key={row.expiry_id} value={row.expiry_date}>
                    {formatDate(row.expiry_date)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label className="text-xs text-muted-foreground">Resolution</Label>
            <div className="flex flex-wrap gap-1">
              {pills.length === 0 ? (
                <span className="text-xs text-muted-foreground">
                  {expiryDate === null ? 'pick an expiry' : 'nothing downloaded yet'}
                </span>
              ) : (
                pills.map((pill) => (
                  <Button
                    key={pill.interval}
                    size="xs"
                    variant={pill.interval === interval ? 'default' : 'outline'}
                    disabled={pill.state !== 'available'}
                    title={pill.reason ?? formatInteger(pill.rows) + ' bars in this expiry'}
                    onClick={() => setChainInterval(pill.interval)}
                  >
                    {pill.interval}
                  </Button>
                ))
              )}
            </div>
          </div>
        </div>
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-3 p-5">
        {underlyingId === null || expiryDate === null ? (
          <EmptyState
            title="Nothing selected"
            description="Choose an underlying and one of its expiries above."
          />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border p-3">
              <div className="flex items-center gap-2">
                <Button
                  size="xs"
                  variant="outline"
                  disabled={timeline.length === 0}
                  onClick={() => void stepDay(-1)}
                >
                  Previous day
                </Button>
                <Badge variant="outline" className="font-mono text-[0.65rem]">
                  {day ?? 'no day'}
                </Badge>
                <Button
                  size="xs"
                  variant="outline"
                  disabled={timeline.length === 0}
                  onClick={() => void stepDay(1)}
                >
                  Next day
                </Button>
              </div>

              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <input
                  type="range"
                  min={0}
                  max={Math.max(0, timeline.length - 1)}
                  step={1}
                  value={scrubIndex}
                  disabled={timeline.length === 0}
                  aria-label="Time of day"
                  onChange={(event) => {
                    const chosen = timeline[Number(event.target.value)]
                    if (chosen !== undefined) {
                      setTs(chosen)
                    }
                  }}
                  // No browser default control on a dark surface: the track and the thumb are
                  // both styled, and the thumb is a step lighter than the track rather than white.
                  className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-muted outline-none disabled:opacity-50 [&::-moz-range-thumb]:size-3.5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-primary [&::-webkit-slider-thumb]:size-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary"
                />
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>{sessionSummary(timeline)}</span>
                  <span className="font-mono">
                    {servedTs === null ? '' : istClock(servedTs) + ' IST'}
                  </span>
                </div>
              </div>

              <div className="flex items-center gap-3 text-xs">
                <span className="text-muted-foreground">
                  {chain.data?.spot === null || chain.data?.spot === undefined
                    ? 'spot not held'
                    : 'spot ' + formatStrike(chain.data.spot)}
                </span>
                <span className="text-muted-foreground">
                  {atmRow === null
                    ? 'no at the money row'
                    : 'ATM ' + formatStrike(atmRow.strike)}
                </span>
              </div>
            </div>

            {chain.isError ? (
              <p className="text-sm text-destructive">{(chain.error as Error).message}</p>
            ) : null}

            <ChainGrid
              className="min-h-0 flex-1"
              rows={rows}
              spot={chain.data?.spot ?? null}
              atmStrike={chain.data?.atm_strike ?? null}
              isLoading={chain.isPending}
              onOpenLeg={openLeg}
            />
          </>
        )}
      </div>
    </div>
  )
}

export default ChainRoute
