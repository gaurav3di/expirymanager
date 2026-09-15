// The DataFeed openalgo-charts reads ExpiryManager's candles through.
//
// getBars only. subscribeBars and subscribeDepth are ABSENT, not stubbed: callers feature detect
// them (`if (feed.subscribeBars)`), so a no-op stub makes a history only feed look live and the
// chart waits forever for a tick that cannot come. Every contract here expired months ago.
//
// THE CLAMP IS THE WHOLE POINT OF THIS FILE. The widget computes its request window as
// `lookbackBars` back from Date.now(): for a 5 minute chart at the default 500 bars that is about
// 42 hours ending now. A contract that expired in March 2025 has no bars in that window at all,
// so a feed that honours `from` and `to` literally answers empty and the terminal says "No bars"
// for every contract we own. That is the default behaviour, not an edge case. So the window is
// slid back to end at the contract's own last bar, keeping the span the widget asked for.
//
// The cache sits INSIDE the clamp, and the order is load bearing. BarCache stores coverage as the
// `from` and `to` of the request that filled the entry, not as the range of the bars that came
// back. Caching a now-relative request that returned March bars would record coverage over a
// window that holds nothing, and the next identical request would be served by slicing that
// entry down to zero bars. So the clamp runs first and the cache only ever sees a window that
// matches the data inside it.

import { withBarCache } from 'openalgo-charts'
import type { Bar, BarsRequest, DataFeed } from 'openalgo-charts'

import { api } from '@/lib/api/client'
import type { QueryParams } from '@/lib/api/client'
import { fyersCodeForInterval } from '@/lib/charts/intervals'

/** Every bar of an expired contract is closed forever, so a cached series can never go stale in
 *  the way a live one does. An hour is not a correctness bound here, it is a memory bound. */
export const EXPIRED_BAR_TTL_MS = 60 * 60 * 1000

/** One entry per symbol and interval. A user flicking between the strikes of one expiry is the
 *  traffic this exists for. */
export const BAR_CACHE_ENTRIES = 48

/** About 120 MB of bar objects at the worst case, which is the ceiling a minute series over a
 *  quarterly future reaches. */
export const BAR_CACHE_BARS = 400_000

/** The bars route answers columnar: `columns` names the fields and `candles` rows are positional.
 *  Nothing here reads a row by position, because the column order is the vendor's and has already
 *  changed once. */
export interface BarsPayload {
  contract_id?: number | null
  symbol?: string | null
  resolution?: string
  res_id?: number
  columns?: string[]
  candles?: unknown[][]
  from_ts?: number | null
  to_ts?: number | null
  first_ts?: number | null
  last_ts?: number | null
  clamped?: boolean
}

export interface OpenInterestPayload {
  contract_id?: number | null
  resolution?: string
  res_id?: number
  points?: Array<[number, number]>
  from_ts?: number | null
  to_ts?: number | null
  clamped?: boolean
}

/** UTC seconds of the first and last bar held for one contract at one resolution. */
export interface ContractWindow {
  firstTs: number
  lastTs: number
}

export interface ClampedWindow {
  from: number
  to: number
  /** True when the window served is not the window asked for. The screen says so rather than
   *  implying the user chose this range. */
  clamped: boolean
}

export interface OpenInterestPoint {
  time: number
  oi: number
}

// ---------------------------------------------------------------------------
// Pure helpers. Exported because they are the part worth testing without a network.
// ---------------------------------------------------------------------------

/**
 * Slides the requested span back so it ends at the contract's last bar.
 *
 *   to   = min(req.to, lastTs)
 *   from = max(firstTs, to - span)
 *
 * A window that already sits inside the contract's life is returned untouched and reports
 * clamped false. A window that ends BEFORE the contract's first bar is also returned untouched,
 * because there empty is the truth: the user panned to a time this contract did not trade, and
 * inventing bars for it by sliding forward would be a lie.
 */
export function clampWindow(
  bounds: ContractWindow,
  from: number | undefined,
  to: number | undefined,
): ClampedWindow {
  if (from === undefined || to === undefined) {
    // The widget always sends both, but the interface allows neither. The contract's whole life
    // is the honest answer to "no window given".
    return { from: bounds.firstTs, to: bounds.lastTs, clamped: false }
  }
  if (to < bounds.firstTs) {
    return { from, to, clamped: false }
  }
  if (to <= bounds.lastTs && from >= bounds.firstTs) {
    return { from, to, clamped: false }
  }
  const span = Math.max(1, to - from)
  const clampedTo = Math.min(to, bounds.lastTs)
  const clampedFrom = Math.max(bounds.firstTs, clampedTo - span)
  return {
    from: clampedFrom,
    to: clampedTo,
    clamped: clampedFrom !== from || clampedTo !== to,
  }
}

/** Number(null) is 0 and Number('') is 0, so a missing price would arrive as a real zero and be
 *  drawn as a bar that collapses the whole price scale. Anything that is not already a finite
 *  number or a numeric string is not a price. */
function numeric(value: unknown): number {
  if (typeof value === 'number') {
    return value
  }
  if (typeof value === 'string' && value.trim() !== '') {
    return Number(value)
  }
  return Number.NaN
}

/**
 * Columnar rows to Bars, read through the `columns` array.
 *
 * Never by position. The historical envelope's column order is the vendor's and open interest was
 * added to the tail of it; a positional reader would put volume in the close the day a column
 * moves. A row missing any of the five required fields is dropped rather than rendered as a bar
 * with a NaN in it, which paints an invisible gap the user cannot see or explain.
 *
 * `Bar.time` is integer UTC seconds. The route already emits epoch seconds as an integer, and
 * Math.floor here is the belt on that brace: a float time silently draws a chart labelled tens of
 * thousands of years out.
 */
export function mapCandles(columns: readonly string[], rows: readonly unknown[][]): Bar[] {
  const index = new Map<string, number>()
  columns.forEach((name, position) => {
    if (!index.has(name)) {
      index.set(name, position)
    }
  })

  const timeAt = index.get('timestamp')
  const openAt = index.get('open')
  const highAt = index.get('high')
  const lowAt = index.get('low')
  const closeAt = index.get('close')
  const volumeAt = index.get('volume')
  if (
    timeAt === undefined ||
    openAt === undefined ||
    highAt === undefined ||
    lowAt === undefined ||
    closeAt === undefined
  ) {
    return []
  }

  const bars: Bar[] = []
  for (const row of rows) {
    const time = numeric(row[timeAt])
    const open = numeric(row[openAt])
    const high = numeric(row[highAt])
    const low = numeric(row[lowAt])
    const close = numeric(row[closeAt])
    if (
      !Number.isFinite(time) ||
      !Number.isFinite(open) ||
      !Number.isFinite(high) ||
      !Number.isFinite(low) ||
      !Number.isFinite(close)
    ) {
      continue
    }
    const bar: Bar = { time: Math.floor(time), open, high, low, close }
    if (volumeAt !== undefined) {
      const volume = numeric(row[volumeAt])
      if (Number.isFinite(volume)) {
        bar.volume = volume
      }
    }
    bars.push(bar)
  }

  bars.sort((left, right) => left.time - right.time)

  // One bar per time. A duplicate key would make the series non-monotonic, which the engine
  // reads as a data error rather than as two ticks.
  const unique: Bar[] = []
  for (const bar of bars) {
    const last = unique[unique.length - 1]
    if (last !== undefined && last.time === bar.time) {
      unique[unique.length - 1] = bar
      continue
    }
    unique.push(bar)
  }
  return unique
}

export function mapOpenInterest(points: ReadonlyArray<readonly unknown[]>): OpenInterestPoint[] {
  const mapped: OpenInterestPoint[] = []
  for (const point of points) {
    const time = numeric(point[0])
    const oi = numeric(point[1])
    if (!Number.isFinite(time) || !Number.isFinite(oi)) {
      continue
    }
    mapped.push({ time: Math.floor(time), oi })
  }
  mapped.sort((left, right) => left.time - right.time)
  return mapped
}

/** The cache and the bounds cache are both keyed this way, so an entry for 1m and an entry for
 *  5m of the same contract never collide. */
export function seriesKey(symbol: string, interval: string): string {
  return symbol.toUpperCase() + '|' + interval
}

// ---------------------------------------------------------------------------
// The feed
// ---------------------------------------------------------------------------

/** Injectable so the feed can be exercised without a server. Mirrors api.get. */
export type GetJson = <T>(path: string, options?: { query?: QueryParams }) => Promise<T>

export interface ExpiryFeedOptions {
  getJson?: GetJson
  ttlMs?: number
  maxEntries?: number
  maxBars?: number
}

/** The window the widget is refused outright: an interval the pill list should never have
 *  offered. Thrown rather than answered empty, so the widget's own data event carries the reason
 *  and the screen can print it. */
export class UnmappedIntervalError extends Error {
  constructor(interval: string) {
    super('No Fyers resolution is mapped to the chart interval ' + interval + '.')
    this.name = 'UnmappedIntervalError'
  }
}

/**
 * The inner feed: it honours `from` and `to` exactly as given and does nothing clever.
 *
 * It exists as its own object because BarCache wraps a DataFeed, and what the cache must see is
 * the already clamped window. Splitting the two is what keeps the cache's coverage honest.
 */
class RawBarsFeed implements DataFeed {
  private readonly _getJson: GetJson
  private readonly _contractIds: Map<string, number>

  constructor(getJson: GetJson, contractIds: Map<string, number>) {
    this._getJson = getJson
    this._contractIds = contractIds
  }

  async getBars(req: BarsRequest): Promise<Bar[]> {
    const payload = await this.fetchPayload(req)
    return mapCandles(payload.columns ?? [], payload.candles ?? [])
  }

  /** The full payload, so the outer feed can learn the contract's bounds from first_ts and
   *  last_ts without a second request. */
  async fetchPayload(req: BarsRequest): Promise<BarsPayload> {
    const resolution = fyersCodeForInterval(req.interval)
    if (resolution === null) {
      throw new UnmappedIntervalError(req.interval)
    }
    const query: QueryParams = { resolution, include_oi: false }
    const contractId = this._contractIds.get(req.symbol.toUpperCase())
    if (contractId === undefined) {
      query.symbol = req.symbol
    } else {
      query.contract_id = contractId
    }
    if (req.from !== undefined) {
      query.from = req.from
    }
    if (req.to !== undefined) {
      query.to = req.to
    }
    return this._getJson<BarsPayload>('/bars', { query })
  }
}

/**
 * The feed the widget is given.
 *
 * Holds the bounds cache, applies the clamp, and delegates the clamped request to the cached
 * inner feed. The extra methods (barsBefore, openInterest, primeBounds, registerContract) are for
 * the chart screen and the open interest indicator; the widget only ever calls getBars.
 */
export class ExpiryManagerDataFeed implements DataFeed {
  private readonly _raw: RawBarsFeed
  private readonly _cached: DataFeed
  private readonly _getJson: GetJson
  private readonly _bounds = new Map<string, ContractWindow>()
  private readonly _contractIds = new Map<string, number>()
  /** Last clamp decision per series, so the screen can say what it is showing. */
  private readonly _lastClamp = new Map<string, boolean>()

  constructor(options: ExpiryFeedOptions = {}) {
    this._getJson = options.getJson ?? ((path, opts) => api.get(path, opts))
    this._raw = new RawBarsFeed(this._getJson, this._contractIds)
    this._cached = withBarCache(this._raw, {
      ttlMs: options.ttlMs ?? EXPIRED_BAR_TTL_MS,
      max: options.maxEntries ?? BAR_CACHE_ENTRIES,
      maxBars: options.maxBars ?? BAR_CACHE_BARS,
    })
  }

  /**
   * Tells the feed which contract a symbol is, so every request can be made by contract_id.
   *
   * The screen already holds this from the row the user clicked. Without it the routes still
   * answer, by symbol, at the cost of one extra lookup per request.
   */
  registerContract(symbol: string, contractId: number): void {
    this._contractIds.set(symbol.toUpperCase(), contractId)
  }

  /** Seeds the bounds cache from the /contracts/{id}/bounds call the screen already makes for
   *  the interval pills, so the first getBars is already clamped and no round trip is spent
   *  discovering that the widget asked for a window forty two hours wide ending today. */
  primeBounds(symbol: string, interval: string, window: ContractWindow): void {
    this._bounds.set(seriesKey(symbol, interval), window)
  }

  boundsFor(symbol: string, interval: string): ContractWindow | null {
    return this._bounds.get(seriesKey(symbol, interval)) ?? null
  }

  /** True when the last load for this series was slid off the window the widget asked for. */
  wasClamped(symbol: string, interval: string): boolean {
    return this._lastClamp.get(seriesKey(symbol, interval)) ?? false
  }

  /** Drops what is known about one contract, for instance after a fresh download extended it. */
  forget(symbol: string): void {
    const prefix = symbol.toUpperCase() + '|'
    for (const key of [...this._bounds.keys()]) {
      if (key.startsWith(prefix)) {
        this._bounds.delete(key)
      }
    }
    for (const key of [...this._lastClamp.keys()]) {
      if (key.startsWith(prefix)) {
        this._lastClamp.delete(key)
      }
    }
  }

  async getBars(req: BarsRequest): Promise<Bar[]> {
    const key = seriesKey(req.symbol, req.interval)
    const known = this._bounds.get(key)

    if (known === undefined) {
      // Bounds unknown: go straight to the network, deliberately bypassing the cache. The window
      // sent is the widget's own now-relative one, the route clamps it server side and reports
      // what it actually served, and caching that request under its asked-for range would poison
      // the entry. The response teaches us the bounds, so this path runs once per series.
      const payload = await this._raw.fetchPayload(req)
      this._learn(key, payload)
      return mapCandles(payload.columns ?? [], payload.candles ?? [])
    }

    const window = clampWindow(known, req.from, req.to)
    this._lastClamp.set(key, window.clamped)
    return this._cached.getBars({ ...req, from: window.from, to: window.to })
  }

  /**
   * Older bars for the history loader, exclusive of `before` and oldest first.
   *
   * Not clamped: this is explicit paging from a timestamp the caller already holds a bar at, so
   * there is nothing to slide.
   */
  async barsBefore(
    symbol: string,
    interval: string,
    before: number,
    count: number,
  ): Promise<Bar[]> {
    const resolution = fyersCodeForInterval(interval)
    if (resolution === null) {
      throw new UnmappedIntervalError(interval)
    }
    const query: QueryParams = { resolution, before, count, include_oi: false }
    const contractId = this._contractIds.get(symbol.toUpperCase())
    if (contractId === undefined) {
      query.symbol = symbol
    } else {
      query.contract_id = contractId
    }
    const payload = await this._getJson<BarsPayload>('/bars/before', { query })
    return mapCandles(payload.columns ?? [], payload.candles ?? [])
  }

  /**
   * The open interest series for the Tier 2 indicator.
   *
   * Bar carries no open interest field, so this is a second series read over the same window and
   * clamped by the same rule on the server, which is what keeps the indicator's points aligned
   * with the candles above it bar for bar.
   */
  async openInterest(
    symbol: string,
    interval: string,
    from?: number,
    to?: number,
  ): Promise<OpenInterestPoint[]> {
    const resolution = fyersCodeForInterval(interval)
    if (resolution === null) {
      throw new UnmappedIntervalError(interval)
    }
    const query: QueryParams = { resolution }
    const contractId = this._contractIds.get(symbol.toUpperCase())
    if (contractId === undefined) {
      query.symbol = symbol
    } else {
      query.contract_id = contractId
    }
    if (from !== undefined && to !== undefined && to > from) {
      query.from = from
      query.to = to
    }
    const payload = await this._getJson<OpenInterestPayload>('/bars/oi', { query })
    return mapOpenInterest(payload.points ?? [])
  }

  private _learn(key: string, payload: BarsPayload): void {
    const firstTs = payload.first_ts
    const lastTs = payload.last_ts
    if (typeof firstTs === 'number' && typeof lastTs === 'number' && lastTs >= firstTs) {
      this._bounds.set(key, { firstTs, lastTs })
    }
    this._lastClamp.set(key, payload.clamped === true)
  }
}

/**
 * One feed for the whole application.
 *
 * Module scope and not a hook: a new instance per render would throw away the bounds cache and
 * the bar cache on every keystroke in the contract picker, and the widget holds whichever feed it
 * was constructed with for its whole life anyway.
 */
export const expiryFeed = new ExpiryManagerDataFeed()
