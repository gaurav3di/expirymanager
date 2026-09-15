// The window the chart asks for, and the window this app can actually answer.
//
// Everything here is about one measured behaviour of the widget: it computes its request window
// as lookbackBars back from Date.now(). Every contract in this app expired months ago, so a feed
// that honours that window literally renders "No bars" for the entire dataset. These tests use
// the genuine now-relative request rather than a tidy fixture, because a fixture with convenient
// numbers would pass while the real thing failed.

import { describe, expect, it } from 'vitest'

import {
  ExpiryManagerDataFeed,
  UnmappedIntervalError,
  clampWindow,
  mapCandles,
  mapOpenInterest,
  seriesKey,
} from '@/lib/charts/expiryFeed'
import type { QueryParams } from '@/lib/api/client'

/** A contract that traded on 26 March 2025, 09:15 to 15:29 IST, one minute bars. */
const FIRST_TS = 1_742_960_700 // 2025-03-26 09:15:00 IST
const LAST_TS = 1_742_983_140 // 2025-03-26 15:29:00 IST

interface Recorded {
  path: string
  query: QueryParams
}

/** A recorder standing in for the API client. Returns one bar per requested minute inside the
 *  contract's life, which is what lets a test assert on what came back rather than on a mock. */
function recorder(options: { first?: number; last?: number } = {}) {
  const first = options.first ?? FIRST_TS
  const last = options.last ?? LAST_TS
  const calls: Recorded[] = []

  const getJson = async <T,>(path: string, opts?: { query?: QueryParams }): Promise<T> => {
    const query = opts?.query ?? {}
    calls.push({ path, query })
    const from = typeof query.from === 'number' ? query.from : first
    const to = typeof query.to === 'number' ? query.to : last
    const candles: number[][] = []
    for (let time = Math.max(from, first); time <= Math.min(to, last); time += 60) {
      candles.push([time, 100, 101, 99, 100.5, 10])
    }
    if (path === '/bars/oi') {
      return {
        points: candles.map((row) => [row[0], 5000]),
        from_ts: from,
        to_ts: to,
        clamped: false,
      } as T
    }
    return {
      contract_id: 42,
      symbol: 'NSE:NIFTY25MAR2723000CE',
      columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume'],
      candles,
      from_ts: from,
      to_ts: to,
      first_ts: first,
      last_ts: last,
      clamped: false,
    } as T
  }

  return { calls, getJson }
}

describe('clampWindow', () => {
  it("slides the widget now-relative window back onto the contract, keeping its span", () => {
    // Exactly what createWidget sends for a 5 minute chart at 500 bars: about 42 hours ending now.
    const now = Math.floor(Date.now() / 1000)
    const span = 500 * 300
    const window = clampWindow({ firstTs: FIRST_TS, lastTs: LAST_TS }, now - span, now)

    expect(window.clamped).toBe(true)
    expect(window.to).toBe(LAST_TS)
    // The span survives, so lookbackBars keeps meaning what it says. It is only truncated where
    // the contract itself is shorter than the span, which it is here.
    expect(window.from).toBe(FIRST_TS)
    expect(window.to - window.from).toBeLessThanOrEqual(span)
  })

  it('keeps the whole span when the contract is longer than the lookback', () => {
    const longFirst = LAST_TS - 90 * 86_400
    const now = Math.floor(Date.now() / 1000)
    const span = 500 * 60
    const window = clampWindow({ firstTs: longFirst, lastTs: LAST_TS }, now - span, now)

    expect(window.to).toBe(LAST_TS)
    expect(window.from).toBe(LAST_TS - span)
    expect(window.clamped).toBe(true)
  })

  it('leaves a window that already sits inside the contract alone', () => {
    const window = clampWindow(
      { firstTs: FIRST_TS, lastTs: LAST_TS },
      FIRST_TS + 600,
      FIRST_TS + 1200,
    )
    expect(window).toEqual({ from: FIRST_TS + 600, to: FIRST_TS + 1200, clamped: false })
  })

  it('does not drag a window forward from before the contract existed', () => {
    // Panning left off the start of the series. Empty is the truth there, and sliding forward
    // would paint bars at times the user did not ask about.
    const from = FIRST_TS - 7200
    const to = FIRST_TS - 3600
    expect(clampWindow({ firstTs: FIRST_TS, lastTs: LAST_TS }, from, to)).toEqual({
      from,
      to,
      clamped: false,
    })
  })

  it("answers the whole life of the contract when no window is given", () => {
    expect(clampWindow({ firstTs: FIRST_TS, lastTs: LAST_TS }, undefined, undefined)).toEqual({
      from: FIRST_TS,
      to: LAST_TS,
      clamped: false,
    })
  })
})

describe('mapCandles', () => {
  it('reads every field through the columns array and never by position', () => {
    // A deliberately unusual order. A positional reader passes the tidy case and puts volume in
    // the close the day the vendor moves a column.
    const bars = mapCandles(
      ['close', 'timestamp', 'volume', 'open', 'low', 'high'],
      [[640, 1_742_967_900, 146_025, 680.5, 608, 708.85]],
    )
    expect(bars).toEqual([
      { time: 1_742_967_900, open: 680.5, high: 708.85, low: 608, close: 640, volume: 146_025 },
    ])
  })

  it('emits integer seconds, because milliseconds draw a chart tens of thousands of years out', () => {
    const bars = mapCandles(
      ['timestamp', 'open', 'high', 'low', 'close', 'volume'],
      [[1_742_967_900.0, 1, 2, 0.5, 1.5, 10]],
    )
    expect(Number.isInteger(bars[0].time)).toBe(true)
  })

  it('drops a row it cannot read rather than painting an invisible NaN gap', () => {
    const bars = mapCandles(
      ['timestamp', 'open', 'high', 'low', 'close', 'volume'],
      [
        [1_742_960_700, 1, 2, 0.5, 1.5, 10],
        [1_742_960_760, null, 2, 0.5, 1.5, 10],
        [1_742_960_820, 1, 2, 0.5, 1.5, 10],
      ],
    )
    expect(bars.map((bar) => bar.time)).toEqual([1_742_960_700, 1_742_960_820])
  })

  it('sorts ascending and keeps one bar per time', () => {
    const bars = mapCandles(
      ['timestamp', 'open', 'high', 'low', 'close'],
      [
        [300, 1, 1, 1, 1],
        [100, 2, 2, 2, 2],
        [300, 3, 3, 3, 3],
      ],
    )
    expect(bars.map((bar) => bar.time)).toEqual([100, 300])
    expect(bars[1].close).toBe(3)
  })

  it('returns nothing when the payload names no timestamp column', () => {
    expect(mapCandles(['open', 'close'], [[1, 2]])).toEqual([])
  })
})

describe('mapOpenInterest', () => {
  it('keeps the pairs as integer seconds and sorts them', () => {
    expect(mapOpenInterest([[200, 7], [100, 5], ['x', 1]])).toEqual([
      { time: 100, oi: 5 },
      { time: 200, oi: 7 },
    ])
  })
})

describe('the feed', () => {
  it('omits subscribeBars and subscribeDepth rather than stubbing them', () => {
    // Callers feature detect these. A no-op stub makes a history only feed look live, and the
    // chart then waits for a tick an expired contract can never produce.
    const feed = new ExpiryManagerDataFeed({ getJson: recorder().getJson })
    expect('subscribeBars' in feed).toBe(false)
    expect('subscribeDepth' in feed).toBe(false)
  })

  it('clamps the very first request when the screen primed the bounds', async () => {
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    feed.primeBounds('NSE:NIFTY25MAR2723000CE', '1m', { firstTs: FIRST_TS, lastTs: LAST_TS })

    const now = Math.floor(Date.now() / 1000)
    const bars = await feed.getBars({
      symbol: 'NSE:NIFTY25MAR2723000CE',
      exchange: 'NSE',
      interval: '1m',
      from: now - 500 * 60,
      to: now,
    })

    expect(calls).toHaveLength(1)
    expect(calls[0].path).toBe('/bars')
    expect(calls[0].query.to).toBe(LAST_TS)
    expect(calls[0].query.from).toBe(FIRST_TS)
    // And the result is real bars, which is the whole point: the same request against an
    // unclamped feed returns nothing at all.
    expect(bars.length).toBeGreaterThan(300)
    expect(bars[0].time).toBe(FIRST_TS)
    expect(bars[bars.length - 1].time).toBe(LAST_TS)
    expect(feed.wasClamped('NSE:NIFTY25MAR2723000CE', '1m')).toBe(true)
  })

  it('learns the bounds from the response when nothing primed them, then clamps', async () => {
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    const now = Math.floor(Date.now() / 1000)

    await feed.getBars({
      symbol: 'NSE:NIFTY25MAR2723000CE',
      exchange: 'NSE',
      interval: '1m',
      from: now - 500 * 60,
      to: now,
    })
    // First request goes out as asked, because there was nothing to clamp against yet.
    expect(calls[0].query.to).toBe(now)
    expect(feed.boundsFor('NSE:NIFTY25MAR2723000CE', '1m')).toEqual({
      firstTs: FIRST_TS,
      lastTs: LAST_TS,
    })

    await feed.getBars({
      symbol: 'NSE:NIFTY25MAR2723000CE',
      exchange: 'NSE',
      interval: '1m',
      from: now - 500 * 60,
      to: now,
    })
    expect(calls[1].query.to).toBe(LAST_TS)
  })

  it('serves the second identical load from the bar cache', async () => {
    // Every bar of an expired contract is closed forever, so this is a free hit rather than a
    // freshness gamble. It only works because the clamp runs before the cache: the cache records
    // coverage as the range of the request it was given, and a now-relative range holding March
    // bars would slice back to nothing on the next read.
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    feed.primeBounds('NSE:NIFTY25MAR2723000CE', '1m', { firstTs: FIRST_TS, lastTs: LAST_TS })
    const now = Math.floor(Date.now() / 1000)
    const request = {
      symbol: 'NSE:NIFTY25MAR2723000CE',
      exchange: 'NSE',
      interval: '1m',
      from: now - 500 * 60,
      to: now,
    }

    const first = await feed.getBars(request)
    const second = await feed.getBars(request)

    expect(calls).toHaveLength(1)
    expect(second.map((bar) => bar.time)).toEqual(first.map((bar) => bar.time))
  })

  it('requests by contract id once the screen has registered one', async () => {
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    feed.registerContract('NSE:NIFTY25MAR2723000CE', 42)
    feed.primeBounds('NSE:NIFTY25MAR2723000CE', '1m', { firstTs: FIRST_TS, lastTs: LAST_TS })

    await feed.getBars({
      symbol: 'NSE:NIFTY25MAR2723000CE',
      exchange: 'NSE',
      interval: '1m',
      from: FIRST_TS,
      to: LAST_TS,
    })

    expect(calls[0].query.contract_id).toBe(42)
    expect(calls[0].query.symbol).toBeUndefined()
    expect(calls[0].query.resolution).toBe('1')
  })

  it('pages older bars through the before route, unclamped', async () => {
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    feed.registerContract('NSE:NIFTY25MAR2723000CE', 42)

    const older = await feed.barsBefore('NSE:NIFTY25MAR2723000CE', '1m', LAST_TS, 500)

    expect(calls[0].path).toBe('/bars/before')
    expect(calls[0].query.before).toBe(LAST_TS)
    expect(calls[0].query.count).toBe(500)
    expect(older.length).toBeGreaterThan(0)
  })

  it('asks the open interest route on the same resolution', async () => {
    const { calls, getJson } = recorder()
    const feed = new ExpiryManagerDataFeed({ getJson })
    feed.registerContract('NSE:NIFTY25MAR2723000CE', 42)

    const points = await feed.openInterest('NSE:NIFTY25MAR2723000CE', '5m', FIRST_TS, LAST_TS)

    expect(calls[0].path).toBe('/bars/oi')
    expect(calls[0].query.resolution).toBe('5')
    expect(points[0]).toEqual({ time: FIRST_TS, oi: 5000 })
  })

  it('refuses an interval with no Fyers resolution instead of silently charting another', async () => {
    const feed = new ExpiryManagerDataFeed({ getJson: recorder().getJson })
    await expect(
      feed.getBars({ symbol: 'NSE:X', exchange: 'NSE', interval: '1w', from: 1, to: 2 }),
    ).rejects.toBeInstanceOf(UnmappedIntervalError)
  })

  it('forgets a contract at every resolution when its download extended it', () => {
    const feed = new ExpiryManagerDataFeed({ getJson: recorder().getJson })
    feed.primeBounds('NSE:A', '1m', { firstTs: 1, lastTs: 2 })
    feed.primeBounds('NSE:A', '5m', { firstTs: 1, lastTs: 2 })
    feed.primeBounds('NSE:B', '1m', { firstTs: 1, lastTs: 2 })

    feed.forget('NSE:A')

    expect(feed.boundsFor('NSE:A', '1m')).toBeNull()
    expect(feed.boundsFor('NSE:A', '5m')).toBeNull()
    expect(feed.boundsFor('NSE:B', '1m')).not.toBeNull()
  })

  it('keys bounds per resolution, because a contract is not bounded the same at each one', () => {
    expect(seriesKey('nse:a', '1m')).toBe('NSE:A|1m')
    expect(seriesKey('NSE:A', '5m')).not.toBe(seriesKey('NSE:A', '1m'))
  })
})
