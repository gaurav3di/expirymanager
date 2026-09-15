// Open interest, as a Tier 2 indicator.
//
// `Bar` has no open interest field and will not grow one: it is not an OHLCV column, it is a
// second series with its own timestamps. The library's answer is the Tier 2 (external data)
// contract, which owns a fetch and merge lifecycle and is wrapped into an ordinary
// IndicatorDescriptor, so the registered result appears in the picker, gets its own pane, gets
// generated settings and is removed like anything else. There is no second indicator runtime.
//
// The alignment rule the runtime applies is last known value: each bar takes the most recent
// external point at or before its own time, never interpolated and never forward looking, and
// bars before the first point read null. That is exactly the right semantics for open interest,
// which is a stock reported per bar rather than a flow.

import { hasIndicator, registerIndicator } from 'openalgo-charts'
import type { IndicatorDescriptor } from 'openalgo-charts'
import { createTier2Indicator } from 'openalgo-charts/indicators'
import type { Tier2Point } from 'openalgo-charts/indicators'

import type { ExpiryManagerDataFeed, OpenInterestPoint } from '@/lib/charts/expiryFeed'
import { expiryFeed } from '@/lib/charts/expiryFeed'

/** The descriptor id. The chart screen adds and removes the instance by this. */
export const OPEN_INTEREST_ID = 'expiry-oi'

export const OPEN_INTEREST_PLOT = 'oi'

/** The settings keys the chart screen writes, and the ones a change of must refetch rather than
 *  merely re-align. Both are the contract's identity: a new symbol is a different series. */
export interface OpenInterestSettings {
  symbol: string
  interval: string
}

/** The narrow slice of the feed this file needs, so a test can hand it a recorder. */
export interface OpenInterestSource {
  openInterest(
    symbol: string,
    interval: string,
    from?: number,
    to?: number,
  ): Promise<OpenInterestPoint[]>
}

/**
 * Reads the series for one window, and falls back to the contract's whole life when the window
 * came back empty.
 *
 * The fallback is not defensive padding. The runtime calls fetch with the first and last time of
 * the bars the chart currently holds, and a settings change (a new contract) can be applied
 * before the new bars have landed, in which case the window handed in belongs to the contract the
 * user just left and holds nothing for this one. Asking again with no window gets the contract's
 * whole open interest history, which the alignment rule then trims to the bars on screen for
 * free. One wasted request in the rare case beats an empty pane under visible candles, which is
 * indistinguishable from "we never downloaded open interest".
 */
export async function fetchOpenInterest(
  source: OpenInterestSource,
  settings: OpenInterestSettings,
  from: number,
  to: number,
): Promise<Tier2Point[]> {
  const windowed =
    to > from ? await source.openInterest(settings.symbol, settings.interval, from, to) : []
  const points =
    windowed.length > 0
      ? windowed
      : await source.openInterest(settings.symbol, settings.interval)
  return points.map((point) => ({ time: point.time, values: { [OPEN_INTEREST_PLOT]: point.oi } }))
}

export function buildOpenInterestIndicator(source: OpenInterestSource): IndicatorDescriptor {
  return createTier2Indicator({
    id: OPEN_INTEREST_ID,
    name: 'Open interest',
    category: 'Options',
    // Its own pane. Open interest is measured in contracts and price in rupees, so sharing the
    // price axis would flatten one of them into the baseline.
    placement: 'pane',
    inputs: [
      { key: 'symbol', type: 'text', label: 'Contract', default: '' },
      { key: 'interval', type: 'text', label: 'Interval', default: '' },
    ],
    plots: [{ key: OPEN_INTEREST_PLOT, type: 'line', title: 'OI' }],
    refetchOn: ['symbol', 'interval'],
    fetch: async ({ settings, from, to }) => {
      const symbol = String(settings.symbol ?? '')
      const interval = String(settings.interval ?? '')
      if (symbol === '' || interval === '') {
        return []
      }
      return fetchOpenInterest(source, { symbol, interval }, from, to)
    },
  })
}

/**
 * Registers the descriptor, once.
 *
 * The registry is a module level Map in one module instance, so registering twice is harmless but
 * pointless, and registering never is what makes chart.addIndicator throw on a page that plainly
 * imported the tier. The chart component calls this before createWidget.
 */
export function registerOpenInterestIndicator(
  source: OpenInterestSource = expiryFeed as ExpiryManagerDataFeed,
): void {
  if (hasIndicator(OPEN_INTEREST_ID)) {
    return
  }
  registerIndicator(buildOpenInterestIndicator(source))
}
