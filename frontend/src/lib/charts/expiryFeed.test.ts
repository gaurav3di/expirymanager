// @vitest-environment jsdom

// The chart window clamp, driven end to end the way the product drives it.
//
// WHY THIS FILE EXISTS. openalgo-charts builds its first request as a now relative span. Measured
// from the shipped bundle, node_modules/openalgo-charts/dist/openalgo-charts.widget.mjs, inside
// Widget.reload():
//
//     const i = Math.floor((this._opts.now ?? Date.now)() / 1e3)
//     { from: o - Math.max(1, Math.round(lookbackBars)) * intervalSeconds, to: o }
//
// Every contract this application serves is expired, so that span lands weeks or months after the
// last bar the contract ever printed. A feed that honours the window literally answers nothing and
// the terminal renders its empty state for the whole dataset. The clamp is the single difference
// between a chart that works and a chart that looks broken, and it is invisible to any test that
// hands the feed a tidy window inside the contract's life.
//
// So every test below asks with the widget's own formula against the real clock, goes through the
// real api client and a stubbed fetch rather than an injected getJson, and asserts the candles
// that came back: how many, at which timestamps, carrying which prices. Never that a function was
// called. The one test that asserts an absence, the missing subscribeBars, asserts the property
// the widget actually feature detects.
//
// Nothing here is a credential and no fixture contains one.

import fs from 'node:fs'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { intervalToSeconds, isKnownInterval } from 'openalgo-charts'

import { RESOLUTIONS } from '@/lib/charts/intervals'
import {
  ExpiryManagerDataFeed,
  UnmappedIntervalError,
  expiryFeed,
} from '@/lib/charts/expiryFeed'

// ---------------------------------------------------------------------------
// The widget's own window, read from the widget's own configuration
// ---------------------------------------------------------------------------

const chartSource = fs.readFileSync(
  path.join(import.meta.dirname, '..', '..', 'components', 'charts', 'ExpiryChart.tsx'),
  'utf8',
)

/** The bar count the application actually hands createWidget. Read out of the component rather
 *  than copied, so raising it moves these tests with it instead of leaving them testing a number
 *  the product no longer uses. */
const LOOKBACK_BARS = Number(/const LOOKBACK_BARS = (\d+)/.exec(chartSource)?.[1])

/** The window the widget will ask this feed for, reproduced from the bundle's reload(). */
function widgetWindow(interval: string, nowSeconds: number): { from: number; to: number } {
  return { from: nowSeconds - Math.max(1, Math.round(LOOKBACK_BARS)) * intervalToSeconds(interval), to: nowSeconds }
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000)
}

// ---------------------------------------------------------------------------
// Contracts that expired, described by their observed sessions
// ---------------------------------------------------------------------------

/** IST wall clock to UTC seconds. India has no daylight saving, so the offset is a constant even
 *  though the session hours it describes are not. */
function ist(year: number, month: number, day: number, hour: number, minute: number): number {
  return Math.floor(Date.UTC(year, month - 1, day, hour - 5, minute - 30) / 1000)
}

/** IST wall clock text, for asserting what a bar's timestamp actually means. */
function istClock(ts: number): string {
  return new Date((ts + 5.5 * 3600) * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

/** 0 is Sunday, 6 is Saturday, in IST. */
function istWeekday(ts: number): number {
  return new Date((ts + 5.5 * 3600) * 1000).getUTCDay()
}

/** One observed session: the time of its first bar and of its last, inclusive. No session length
 *  is assumed anywhere, because the sessions in this dataset are not all the same length and two
 *  of them fall at a weekend. */
interface Session {
  first: number
  last: number
}

interface Contract {
  symbol: string
  contractId: number
  sessions: readonly Session[]
}

/**
 * A weekly NIFTY option that expired on 27 March 2025, roughly eighteen months before this test
 * runs. Three sessions, each 09:15 to 15:29, which is what a 15:30 close looked like then.
 */
const MARCH_2025: Contract = {
  symbol: 'NSE:NIFTY25MAR2723000CE',
  contractId: 4207,
  sessions: [
    { first: ist(2025, 3, 25, 9, 15), last: ist(2025, 3, 25, 15, 29) },
    { first: ist(2025, 3, 26, 9, 15), last: ist(2025, 3, 26, 15, 29) },
    { first: ist(2025, 3, 27, 9, 15), last: ist(2025, 3, 27, 15, 29) },
  ],
}

/**
 * A contract from after the close moved, with a Saturday special session in the middle of it.
 *
 * SESSION HOURS ARE NOT CONSTANTS IN THIS DATASET. The NSE derivatives close moved from 15:30 to
 * 15:40 on 2026-08-03, measured from live candles, and special sessions have run on a Saturday.
 * A clamp that assumed a 375 minute weekday session would drop the 15:39 bars and the whole
 * Saturday, and the loss would look like ordinary missing data rather than like a bug.
 */
const AUGUST_2026: Contract = {
  symbol: 'NSE:BANKNIFTY26AUG2755000PE',
  contractId: 5511,
  sessions: [
    { first: ist(2026, 8, 3, 9, 15), last: ist(2026, 8, 3, 15, 39) },
    { first: ist(2026, 8, 8, 10, 0), last: ist(2026, 8, 8, 11, 30) },
    { first: ist(2026, 8, 27, 9, 15), last: ist(2026, 8, 27, 15, 39) },
  ],
}

/** Every bar time this contract holds at one interval, ascending. An interval longer than a
 *  session leaves that session with the single bar it opened. */
function barTimes(contract: Contract, intervalSeconds: number): number[] {
  const times: number[] = []
  for (const session of contract.sessions) {
    for (let time = session.first; time <= session.last; time += intervalSeconds) {
      times.push(time)
    }
  }
  return times
}

function firstTsOf(contract: Contract, intervalSeconds: number): number {
  const times = barTimes(contract, intervalSeconds)
  return times[0]
}

function lastTsOf(contract: Contract, intervalSeconds: number): number {
  const times = barTimes(contract, intervalSeconds)
  return times[times.length - 1]
}

/** Deterministic prices, so a test can assert the value that came back rather than only its
 *  shape. The sequence number is the bar's position in the contract's whole life. */
function priceAt(seq: number): { open: number; high: number; low: number; close: number; volume: number; oi: number } {
  const open = 100 + seq
  return { open, high: open + 2, low: open - 1, close: open + 0.5, volume: 1000 + seq, oi: 5000 + seq }
}

// ---------------------------------------------------------------------------
// A stand in for the bars routes, over the real fetch the client calls
// ---------------------------------------------------------------------------

/**
 * Column order is deliberately NOT the canonical one.
 *
 * The historical envelope's column order is the vendor's and open interest was appended to it
 * once already. A reader that took fields by position would put the volume in the close here, and
 * would draw a chart that is wrong rather than one that is empty.
 */
const COLUMNS = ['open', 'high', 'timestamp', 'low', 'volume', 'close', 'open_interest'] as const

function rowFor(time: number, seq: number): unknown[] {
  const price = priceAt(seq)
  return [price.open, price.high, time, price.low, price.volume, price.close, price.oi]
}

/**
 * The server side clamp, mirrored from expirymanager/api/v1/bars.py `_clamp`.
 *
 * Written out again here rather than imported from the frontend, so that the frontend clamp is
 * compared against the backend's rule instead of against itself.
 */
function serverClamp(
  firstTs: number,
  lastTs: number,
  from: number | null,
  to: number | null,
): { from: number; to: number; clamped: boolean } {
  if (from === null || to === null) {
    return { from: firstTs, to: lastTs, clamped: from !== null || to !== null }
  }
  if (to < firstTs) {
    return { from, to, clamped: false }
  }
  const span = Math.max(1, to - from)
  const end = Math.min(to, lastTs)
  const start = Math.max(firstTs, end - span)
  return { from: start, to: end, clamped: start !== from || end !== to }
}

interface Recorded {
  method: string
  path: string
  query: URLSearchParams
  headers: Record<string, string>
}

type ServerMode = 'literal' | 'clamping'

/**
 * Stubs fetch with the two bars routes.
 *
 * `literal` honours the window exactly as asked and clamps nothing. That is not what the backend
 * does, and that is the point: with a literal server the only thing that can put candles on the
 * screen is the clamp in the feed, so a test that gets candles has proved the clamp and not the
 * fixture. `clamping` mirrors the real route, and is used for the path where the feed has not
 * learned the contract's bounds yet.
 */
function serveBars(contract: Contract, mode: ServerMode) {
  const calls: Recorded[] = []

  const stub = vi.fn(async (input: unknown, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), 'http://127.0.0.1:8000')
    const headerBag = (init?.headers ?? {}) as Record<string, string>
    calls.push({
      method: init?.method ?? 'GET',
      path: url.pathname,
      query: url.searchParams,
      headers: headerBag,
    })

    const resolutionCode = url.searchParams.get('resolution') ?? '1'
    const spec = RESOLUTIONS.find((row) => row.fyersCode === resolutionCode)
    if (spec === undefined) {
      return jsonResponse({ error: { code: 'unknown_resolution', message: 'no such resolution' } }, 422)
    }
    const times = barTimes(contract, spec.seconds)
    const firstTs = times[0]
    const lastTs = times[times.length - 1]
    const seqOf = new Map(times.map((time, index) => [time, index]))

    if (url.pathname === '/api/v1/bars/before') {
      const before = Number(url.searchParams.get('before'))
      const count = Number(url.searchParams.get('count'))
      const older = times.filter((time) => time < before).slice(-count)
      return jsonResponse({
        contract_id: contract.contractId,
        symbol: contract.symbol,
        resolution: resolutionCode,
        res_id: spec.resId,
        columns: [...COLUMNS],
        candles: older.map((time) => rowFor(time, seqOf.get(time) ?? 0)),
        first_ts: firstTs,
        last_ts: lastTs,
      })
    }

    const askedFrom = url.searchParams.has('from') ? Number(url.searchParams.get('from')) : null
    const askedTo = url.searchParams.has('to') ? Number(url.searchParams.get('to')) : null
    const window =
      mode === 'clamping'
        ? serverClamp(firstTs, lastTs, askedFrom, askedTo)
        : {
            from: askedFrom ?? firstTs,
            to: askedTo ?? lastTs,
            clamped: false,
          }
    const inWindow = times.filter((time) => time >= window.from && time <= window.to)

    if (url.pathname === '/api/v1/bars/oi') {
      return jsonResponse({
        contract_id: contract.contractId,
        resolution: resolutionCode,
        res_id: spec.resId,
        points: inWindow.map((time) => [time, priceAt(seqOf.get(time) ?? 0).oi]),
        from_ts: window.from,
        to_ts: window.to,
        clamped: window.clamped,
      })
    }

    if (url.pathname !== '/api/v1/bars') {
      return jsonResponse({ error: { code: 'not_found', message: 'no such route' } }, 404)
    }

    return jsonResponse({
      contract_id: contract.contractId,
      symbol: contract.symbol,
      resolution: resolutionCode,
      res_id: spec.resId,
      columns: [...COLUMNS],
      candles: inWindow.map((time) => rowFor(time, seqOf.get(time) ?? 0)),
      from_ts: window.from,
      to_ts: window.to,
      first_ts: firstTs,
      last_ts: lastTs,
      clamped: window.clamped,
    })
  })

  vi.stubGlobal('fetch', stub)
  return { calls }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A feed with nothing injected, so the request really does travel through the api client. */
function newFeed(): ExpiryManagerDataFeed {
  return new ExpiryManagerDataFeed()
}

function primed(contract: Contract, interval: string, seconds: number): ExpiryManagerDataFeed {
  const feed = newFeed()
  feed.registerContract(contract.symbol, contract.contractId)
  feed.primeBounds(contract.symbol, interval, {
    firstTs: firstTsOf(contract, seconds),
    lastTs: lastTsOf(contract, seconds),
  })
  return feed
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------

describe('the window the widget asks for', () => {
  it('is built from a bar count the component really passes to createWidget', () => {
    expect(Number.isInteger(LOOKBACK_BARS)).toBe(true)
    expect(LOOKBACK_BARS).toBeGreaterThan(0)
    expect(chartSource).toContain('lookbackBars: LOOKBACK_BARS')
  })

  it('lands entirely after the last bar of a contract that expired months ago', () => {
    // The premise of every test below, asserted rather than assumed, and against the real clock.
    // If this ever fails the contract has stopped being expired and the fixture needs replacing.
    const lastTs = lastTsOf(MARCH_2025, 60)
    const asked = widgetWindow('1m', nowSeconds())

    expect(asked.from).toBeGreaterThan(lastTs)
    expect(barTimes(MARCH_2025, 60).filter((t) => t >= asked.from && t <= asked.to)).toHaveLength(0)
  })

  it('offers only intervals the chart library itself resolves, at the seconds it resolves them to', () => {
    // The seconds are the multiplier in the window formula above. A wrong one here is a window of
    // the wrong length, and an unknown code makes createWidget throw before a chart is ever built.
    for (const spec of RESOLUTIONS) {
      expect(isKnownInterval(spec.chartInterval), spec.chartInterval).toBe(true)
      expect(intervalToSeconds(spec.chartInterval), spec.chartInterval).toBe(spec.seconds)
    }
  })
})

describe('the clamp', () => {
  it('turns the widget request into candles at every interval the pills offer', async () => {
    for (const spec of RESOLUTIONS) {
      const { calls } = serveBars(MARCH_2025, 'literal')
      const feed = primed(MARCH_2025, spec.chartInterval, spec.seconds)
      const firstTs = firstTsOf(MARCH_2025, spec.seconds)
      const lastTs = lastTsOf(MARCH_2025, spec.seconds)

      const bars = await feed.getBars({
        symbol: MARCH_2025.symbol,
        exchange: 'NSE',
        interval: spec.chartInterval,
        ...widgetWindow(spec.chartInterval, nowSeconds()),
      })

      // The whole deliverable in four assertions: a now relative request against a 2025 contract
      // came back with bars, they are the contract's own bars, and the newest of them is the last
      // bar the contract ever printed, which is where the chart opens.
      expect(bars.length, spec.chartInterval).toBeGreaterThan(0)
      expect(bars[0].time, spec.chartInterval).toBeGreaterThanOrEqual(firstTs)
      expect(bars[bars.length - 1].time, spec.chartInterval).toBe(lastTs)
      for (const bar of bars) {
        expect(Number.isInteger(bar.time), spec.chartInterval).toBe(true)
      }
      // Ascending and one bar per time, the two rules the engine reads as a data error.
      const times = bars.map((bar) => bar.time)
      expect([...times].sort((a, b) => a - b), spec.chartInterval).toEqual(times)
      expect(new Set(times).size, spec.chartInterval).toBe(times.length)
      expect(calls[0].query.get('resolution'), spec.chartInterval).toBe(spec.fyersCode)

      vi.unstubAllGlobals()
    }
  })

  it('is the whole difference: the same window unclamped answers No bars', async () => {
    const { calls } = serveBars(MARCH_2025, 'literal')
    const asked = widgetWindow('1m', nowSeconds())
    const request = {
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...asked,
    }

    // A feed that knows nothing, against a server that clamps nothing, is the unclamped case.
    const unclamped = await newFeed().getBars(request)
    // The same request through a feed the screen primed.
    const clamped = await primed(MARCH_2025, '1m', 60).getBars(request)

    expect(unclamped).toHaveLength(0)
    expect(clamped.length).toBeGreaterThan(0)
    // Both really asked the same route. The difference is the window, not the request path.
    expect(calls[0].path).toBe('/api/v1/bars')
    expect(Number(calls[0].query.get('to'))).toBe(asked.to)
    expect(Number(calls[1].query.get('to'))).toBe(lastTsOf(MARCH_2025, 60))
  })

  it('keeps the span the widget asked for and hands back every bar inside it', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const lastTs = lastTsOf(MARCH_2025, 60)

    const bars = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    // The window is LOOKBACK_BARS minutes long, ending at the last bar. That is wall clock time,
    // not bar count: it spans two overnight gaps, so it holds far fewer than LOOKBACK_BARS bars,
    // and the count is the number of bars the contract really printed in that span.
    const expected = barTimes(MARCH_2025, 60).filter(
      (time) => time >= lastTs - LOOKBACK_BARS * 60 && time <= lastTs,
    )
    expect(bars).toHaveLength(expected.length)
    expect(bars.map((bar) => bar.time)).toEqual(expected)
    expect(bars.length).toBeGreaterThan(0)
  })

  it('reads every price through the columns array, not by position', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)

    const bars = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    // The fixture's columns are ordered open, high, timestamp, low, volume, close, open_interest.
    // A positional reader would report the open as the timestamp and the volume as the low.
    const seq = barTimes(MARCH_2025, 60).indexOf(bars[0].time)
    const price = priceAt(seq)
    expect(bars[0]).toEqual({
      time: bars[0].time,
      open: price.open,
      high: price.high,
      low: price.low,
      close: price.close,
      volume: price.volume,
    })
  })

  it('says so, so the screen can label a window it slid rather than imply the user chose it', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const lastTs = lastTsOf(MARCH_2025, 60)

    await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })
    expect(feed.wasClamped(MARCH_2025.symbol, '1m')).toBe(true)

    await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      from: lastTs - 3600,
      to: lastTs,
    })
    expect(feed.wasClamped(MARCH_2025.symbol, '1m')).toBe(false)
  })

  it('does not drag a window forward from before the contract existed', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const firstTs = firstTsOf(MARCH_2025, 60)

    const bars = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      from: firstTs - 4 * 86400,
      to: firstTs - 86400,
    })

    // Empty is the truth here. Sliding this forward would invent a session the contract did not
    // trade, and the user panning left would watch the same bars reappear forever.
    expect(bars).toHaveLength(0)
    expect(feed.wasClamped(MARCH_2025.symbol, '1m')).toBe(false)
  })
})

describe('the first load, before anything has primed the feed', () => {
  it('still answers with candles, because the route clamps too', async () => {
    const { calls } = serveBars(MARCH_2025, 'clamping')
    const feed = newFeed()
    const lastTs = lastTsOf(MARCH_2025, 60)

    const bars = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    expect(bars.length).toBeGreaterThan(0)
    expect(bars[bars.length - 1].time).toBe(lastTs)
    expect(calls).toHaveLength(1)
    // And the response taught the feed where the contract lives, so the next request is clamped
    // here rather than costing another round trip to discover the same thing.
    expect(feed.boundsFor(MARCH_2025.symbol, '1m')).toEqual({
      firstTs: firstTsOf(MARCH_2025, 60),
      lastTs,
    })
    expect(feed.wasClamped(MARCH_2025.symbol, '1m')).toBe(true)
  })

  it('clamps the second load itself, and asks for a window the server has to change nothing about', async () => {
    const { calls } = serveBars(MARCH_2025, 'clamping')
    const feed = newFeed()
    const request = {
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    }

    await feed.getBars(request)
    const second = await feed.getBars({ ...request, from: request.from - 7200 })

    expect(second.length).toBeGreaterThan(0)
    expect(Number(calls[1].query.get('to'))).toBe(lastTsOf(MARCH_2025, 60))
    expect(Number(calls[1].query.get('from'))).toBeGreaterThanOrEqual(firstTsOf(MARCH_2025, 60))
  })

  it('goes back to discovering the bounds after a download extended the contract', async () => {
    const { calls } = serveBars(MARCH_2025, 'clamping')
    const feed = newFeed()
    const request = {
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    }

    await feed.getBars(request)
    feed.forget(MARCH_2025.symbol)
    const after = await feed.getBars(request)

    expect(feed.boundsFor(MARCH_2025.symbol, '1m')).not.toBeNull()
    expect(after.length).toBeGreaterThan(0)
    // The second request went out as the widget asked, which is what lets a freshly extended
    // contract report its new last bar instead of being pinned to the old one.
    expect(Number(calls[1].query.get('to'))).toBe(request.to)
  })
})

describe('the bar cache, which sits inside the clamp', () => {
  it('answers a repeated load from memory with the same bars, not with nothing', async () => {
    // The ordering hazard this pins: the cache records coverage as the window of the request that
    // filled it. Caching the widget's now relative window against March bars would record coverage
    // over a window that holds nothing, and the next identical load would slice down to zero bars.
    const { calls } = serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const request = {
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    }

    const first = await feed.getBars(request)
    const second = await feed.getBars(request)

    expect(first.length).toBeGreaterThan(0)
    expect(second.map((bar) => bar.time)).toEqual(first.map((bar) => bar.time))
    expect(second.map((bar) => bar.close)).toEqual(first.map((bar) => bar.close))
    expect(calls).toHaveLength(1)
  })

  it('serves a narrower window out of a wider one and still fetches a wider one', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const lastTs = lastTsOf(MARCH_2025, 60)
    const ask = (from: number) =>
      feed.getBars({ symbol: MARCH_2025.symbol, exchange: 'NSE', interval: '1m', from, to: lastTs })

    const narrow = await ask(lastTs - 1800)
    const wide = await ask(firstTsOf(MARCH_2025, 60))
    const narrowAgain = await ask(lastTs - 1800)

    expect(narrow).toHaveLength(31)
    expect(wide.length).toBeGreaterThan(narrow.length)
    expect(narrowAgain.map((bar) => bar.time)).toEqual(narrow.map((bar) => bar.time))
  })

  it('keeps two contracts apart', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const lastTs = lastTsOf(MARCH_2025, 60)
    const mine = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      from: lastTs - 600,
      to: lastTs,
    })

    vi.unstubAllGlobals()
    serveBars(AUGUST_2026, 'clamping')
    feed.registerContract(AUGUST_2026.symbol, AUGUST_2026.contractId)
    const other = await feed.getBars({
      symbol: AUGUST_2026.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    expect(mine.length).toBeGreaterThan(0)
    expect(other.length).toBeGreaterThan(0)
    expect(other[other.length - 1].time).toBe(lastTsOf(AUGUST_2026, 60))
    expect(other[0].time).not.toBe(mine[0].time)
  })
})

describe('session hours, which are not constants in this dataset', () => {
  it('returns the 15:39 bars of a contract whose close had moved', async () => {
    serveBars(AUGUST_2026, 'literal')
    const feed = primed(AUGUST_2026, '1m', 60)

    const bars = await feed.getBars({
      symbol: AUGUST_2026.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    const last = bars[bars.length - 1]
    expect(istClock(last.time)).toBe('2026-08-27 15:39')
    // A feed that assumed a 15:30 close would stop ten bars earlier, on a chart that looks
    // complete.
    expect(bars.some((bar) => istClock(bar.time).endsWith('15:35'))).toBe(true)
  })

  it('returns a Saturday special session rather than treating the weekend as closed', async () => {
    serveBars(AUGUST_2026, 'literal')
    const feed = primed(AUGUST_2026, '1m', 60)
    const saturday = AUGUST_2026.sessions[1]

    const bars = await feed.getBars({
      symbol: AUGUST_2026.symbol,
      exchange: 'NSE',
      interval: '1m',
      from: saturday.first - 3600,
      to: saturday.last + 3600,
    })

    expect(istWeekday(saturday.first)).toBe(6)
    expect(bars).toHaveLength(91)
    expect(istClock(bars[0].time)).toBe('2026-08-08 10:00')
    expect(istClock(bars[bars.length - 1].time)).toBe('2026-08-08 11:30')
  })

  it('names no session hour, no session length and no weekday anywhere in the feed', () => {
    const source = fs.readFileSync(path.join(import.meta.dirname, 'expiryFeed.ts'), 'utf8')
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
    for (const forbidden of ['09:15', '15:30', '15:40', '15:29', '375', 'Saturday', 'weekend']) {
      expect(code, forbidden).not.toContain(forbidden)
    }
    // 86400 and 3600 are units, not session facts, and would be legitimate. The numbers that
    // would not be are a session length in seconds.
    for (const forbidden of ['22500', '23400', '33300']) {
      expect(code, forbidden).not.toContain(forbidden)
    }
  })
})

describe('the feed object the widget is handed', () => {
  it('omits subscribeBars and subscribeDepth rather than stubbing them', () => {
    // The widget feature detects these: `if (feed.subscribeBars)`. A no op stub makes a history
    // only feed look live and the chart then waits forever for a tick an expired contract cannot
    // produce. Absence is the contract, so absence is what is asserted, including up the
    // prototype chain, which is where a class method would live.
    for (const feed of [expiryFeed, newFeed()]) {
      expect('subscribeBars' in feed).toBe(false)
      expect('subscribeDepth' in feed).toBe(false)
      expect(typeof (feed as { subscribeBars?: unknown }).subscribeBars).toBe('undefined')
      expect(Object.getOwnPropertyNames(Object.getPrototypeOf(feed))).not.toContain('subscribeBars')
    }
    expect(typeof expiryFeed.getBars).toBe('function')
  })

  it('is the same instance for the whole application, so the caches survive a re-render', () => {
    expect(expiryFeed).toBeInstanceOf(ExpiryManagerDataFeed)
  })

  it('asks by contract id, on a safe method that carries no csrf token', async () => {
    const { calls } = serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)

    await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      ...widgetWindow('1m', nowSeconds()),
    })

    expect(calls[0].method).toBe('GET')
    expect(calls[0].query.get('contract_id')).toBe(String(MARCH_2025.contractId))
    expect(calls[0].query.get('symbol')).toBeNull()
    expect(calls[0].query.get('include_oi')).toBe('false')
    expect(calls[0].headers['X-CSRF-Token']).toBeUndefined()
  })

  it('pages older bars through the before route, unclamped, oldest first', async () => {
    const { calls } = serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const times = barTimes(MARCH_2025, 60)
    const opened = times[times.length - 1] - 600

    const older = await feed.barsBefore(MARCH_2025.symbol, '1m', opened, 40)

    expect(calls[0].path).toBe('/api/v1/bars/before')
    expect(older).toHaveLength(40)
    expect(older[older.length - 1].time).toBeLessThan(opened)
    expect(older.map((bar) => bar.time)).toEqual(times.filter((t) => t < opened).slice(-40))
  })

  it('reads open interest over the same window, point for point with the candles', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = primed(MARCH_2025, '1m', 60)
    const lastTs = lastTsOf(MARCH_2025, 60)

    const bars = await feed.getBars({
      symbol: MARCH_2025.symbol,
      exchange: 'NSE',
      interval: '1m',
      from: lastTs - 1800,
      to: lastTs,
    })
    const points = await feed.openInterest(MARCH_2025.symbol, '1m', lastTs - 1800, lastTs)

    expect(points).toHaveLength(bars.length)
    expect(points.map((point) => point.time)).toEqual(bars.map((bar) => bar.time))
    expect(points[0].oi).toBe(priceAt(barTimes(MARCH_2025, 60).indexOf(bars[0].time)).oi)
  })

  it('refuses an interval with no Fyers resolution, in words the widget can print', async () => {
    serveBars(MARCH_2025, 'literal')
    const feed = newFeed()

    const thrown = await feed
      .getBars({ symbol: MARCH_2025.symbol, exchange: 'NSE', interval: '1w', from: 1, to: 2 })
      .catch((error: unknown) => error)

    // The widget puts this message straight into its toast and its data event, so it has to name
    // the interval rather than say that something went wrong.
    expect(thrown).toBeInstanceOf(UnmappedIntervalError)
    expect((thrown as Error).message).toContain('1w')
    expect((thrown as Error).message).toContain('No Fyers resolution')
  })
})
