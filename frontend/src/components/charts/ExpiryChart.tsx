import { useEffect, useRef } from 'react'
import { createWidget } from 'openalgo-charts/widget'
import type { Widget } from 'openalgo-charts/widget'
import type { IndicatorApi } from 'openalgo-charts'
// Bare side effect import. Without it the indicator registry is empty, the picker opens with
// nothing in it and chart.addIndicator throws on a page that plainly asked for indicators. It is
// listed in the package's own sideEffects array, so no bundler drops it.
import 'openalgo-charts/indicators'

import { expiryFeed } from '@/lib/charts/expiryFeed'
import { OPEN_INTEREST_ID, registerOpenInterestIndicator } from '@/components/charts/oiIndicator'

// The terminal, wrapped in exactly as much React as it needs and no more.
//
// The rules below are the library's own React guidance and every one of them has a failure that
// motivates it:
//
//  - createWidget runs in a mount effect with an EMPTY dependency array. The constructor reads
//    clientWidth, calls getComputedStyle, writes inline styles and installs a ResizeObserver, so
//    it cannot run in the component body, in useMemo, or before the ref is attached.
//  - The handle lives in a useRef, never in useState. It is a mutable object graph with a running
//    animation frame loop; putting it in state renders on every internal mutation and re-creates
//    it on every parent render.
//  - Symbol, interval, theme and chart type are NOT dependencies of that effect. They are pushed
//    into the live instance through the imperative setters from separate effects, because
//    rebuilding the terminal would throw away the user's indicators, drawings and viewport.
//  - destroy() in the cleanup. It removes the .oac-widget root the constructor appended, which is
//    also what makes React strict mode's double invoke a non-event.
//  - Callbacks reach the component through refs rather than closures, because the widget's
//    callback bag is built before the constructor returns.
//
// The container is sized in CSS with a resolved height. `height: 100%` inside an auto height
// parent is zero pixels and nothing renders at all.

export interface ExpiryChartProps {
  /** The full Fyers symbol, for example NSE:NIFTY25MAR2723000CE. The widget upper cases it. */
  symbol: string
  exchange: string
  /** Chart interval code, already validated against the registry by the pill list. */
  interval: string
  /** The pill list. Every entry must be a code the registry knows or createWidget throws. */
  intervals: readonly string[]
  theme: 'dark' | 'light'
  /** Adds and removes the open interest pane. */
  showOpenInterest?: boolean
  /** Fires after every load with the bar count, and the error text when one failed. */
  onData?: (bars: number, error?: string) => void
  /** Fires when the user changes the interval from the widget's own pills, so the screen's pill
   *  row and the widget never disagree about which one is active. */
  onIntervalChange?: (interval: string) => void
  className?: string
}

/** Bars per load. The feed slides this window onto the contract's own life, so it is a bar count
 *  and not a date range. */
const LOOKBACK_BARS = 500

/** Bars fetched per history page when the user pans off the left edge. */
const HISTORY_PAGE = 500

/** The logical range threshold the engine pages at is fixed; this is only how many bars we ask
 *  for when it fires. */
export function oldestBarTime(bars: ReadonlyArray<{ time: number }>): number | null {
  return bars.length > 0 ? bars[0].time : null
}

export function ExpiryChart({
  symbol,
  exchange,
  interval,
  intervals,
  theme,
  showOpenInterest = false,
  onData,
  onIntervalChange,
  className,
}: ExpiryChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const widgetRef = useRef<Widget | null>(null)
  const oiRef = useRef<IndicatorApi | null>(null)

  // Latest props for the imperative callbacks, which must not become effect dependencies.
  //
  // Written in an effect rather than during render, and declared before the mount effect so this
  // one runs first: the widget does not exist until the mount effect has run, so nothing can read
  // a stale value in between.
  const onDataRef = useRef(onData)
  const onIntervalRef = useRef(onIntervalChange)
  const symbolRef = useRef(symbol)
  const intervalRef = useRef(interval)
  useEffect(() => {
    onDataRef.current = onData
    onIntervalRef.current = onIntervalChange
    symbolRef.current = symbol
    intervalRef.current = interval
  })

  // Mount only. See the note above: this array stays empty.
  useEffect(() => {
    const element = containerRef.current
    if (element === null) {
      return
    }

    // Before createWidget, so the descriptor is in the registry when the picker is built.
    registerOpenInterestIndicator(expiryFeed)

    let alive = true
    const widget = createWidget(element, {
      feed: expiryFeed,
      symbol: symbolRef.current,
      exchange,
      interval: intervalRef.current,
      intervals: [...intervals],
      chartType: 'candlestick',
      theme,
      // IANA name, never a fixed offset. Stated explicitly even though it is the library default,
      // so the intent is on the page rather than in a dependency's changelog.
      timezone: 'Asia/Kolkata',
      lookbackBars: LOOKBACK_BARS,
      // A namespace rather than true: two widgets sharing the default namespace share a layout.
      persist: 'expiry-chart',
      topbar: true,
      statusline: true,
      rail: true,
      indicators: true,
    })
    widgetRef.current = widget

    const offData = widget.on('data', (event) => {
      if (alive) {
        onDataRef.current?.(event.bars, event.error)
      }
    })
    const offInterval = widget.on('interval', (event) => {
      if (alive) {
        onIntervalRef.current?.(event.interval)
      }
    })

    // Infinite history. historyLoadComplete() is mandatory on EVERY exit path, including the one
    // where nothing came back and the one where the request threw: a latch suppresses re-entry
    // until it is called, so skipping it once kills paging for the rest of the session.
    widget.chart.setHistoryLoader(() => {
      void (async () => {
        try {
          const current = widgetRef.current
          if (current === null || current.isDestroyed) {
            return
          }
          const oldest = oldestBarTime(current.series.getData())
          if (oldest === null) {
            return
          }
          const older = await expiryFeed.barsBefore(
            symbolRef.current,
            intervalRef.current,
            oldest,
            HISTORY_PAGE,
          )
          const live = widgetRef.current
          if (live === null || live.isDestroyed || older.length === 0) {
            return
          }
          // prependData shifts every logical index, so the viewport is restored by hand or the
          // chart jumps back by exactly the number of bars that just arrived.
          const before = live.chart.getVisibleLogicalRange()
          live.series.prependData(older)
          live.chart.setVisibleLogicalRange({
            from: before.from + older.length,
            to: before.to + older.length,
          })
        } finally {
          widgetRef.current?.chart.historyLoadComplete()
        }
      })()
    })

    return () => {
      alive = false
      offData()
      offInterval()
      oiRef.current = null
      widget.destroy()
      widgetRef.current = null
    }
    // oxlint-disable-next-line exhaustive-deps
  }, [])

  // Drive the live instance. Each setter no-ops when the value is unchanged, so an unrelated
  // parent render costs nothing.
  useEffect(() => {
    widgetRef.current?.setSymbol(symbol, exchange)
  }, [symbol, exchange])

  useEffect(() => {
    widgetRef.current?.setInterval(interval)
  }, [interval])

  useEffect(() => {
    widgetRef.current?.setTheme(theme)
  }, [theme])

  // Open interest. Added and removed rather than hidden, so a chart with the pane off is not
  // quietly fetching a second series on every load.
  useEffect(() => {
    const widget = widgetRef.current
    if (widget === null || widget.isDestroyed) {
      return
    }
    if (!showOpenInterest) {
      oiRef.current?.remove()
      oiRef.current = null
      return
    }
    const settings = { symbol, interval }
    if (oiRef.current === null) {
      oiRef.current = widget.chart.addIndicator(OPEN_INTEREST_ID, settings)
      return
    }
    // A settings patch rather than a remove and re-add: the descriptor lists both keys in
    // refetchOn, so this is what makes the pane follow the contract.
    oiRef.current.setSettings(settings)
  }, [showOpenInterest, symbol, interval])

  return (
    <div
      ref={containerRef}
      data-testid="expiry-chart"
      // A resolved height. h-full alone inside an auto height parent is zero pixels and the
      // canvas never paints.
      className={'relative h-full min-h-[480px] w-full ' + (className ?? '')}
    />
  )
}

export default ExpiryChart
