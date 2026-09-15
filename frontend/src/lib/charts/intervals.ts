// Chart interval codes and Fyers resolution codes, and the pill list the chart is allowed to
// offer.
//
// Three vocabularies meet here and none of them is allowed to be guessed at:
//
//   dim_resolution.fyers_code      what the download asked Fyers for: "1", "5", "5S", "D"
//   dim_resolution.chart_interval  what openalgo-charts calls the same thing: "1m", "5m", "5s", "1d"
//   dim_resolution.res_id          what candle_coverage and contract_bounds key on
//
// The backend already carries all three on every bounds row, so the API's own chart_interval is
// the authority wherever it is present. The table below exists for the rows where it is not, and
// as the reverse map: a pill has to be turned back into a Fyers resolution code to build the bars
// request. It mirrors the seed block in db/duck_schema.sql exactly, and a test asserts that.
//
// One measured rule from the chart library that must not be softened: upper case M is NOT
// minutes. isKnownInterval refuses it, setInterval throws UnknownIntervalError on it, and a pill
// list containing it makes createWidget throw before the chart is ever built. So every code that
// reaches the widget is validated through the library's own registry rather than through a
// regular expression written here.

import { isKnownInterval } from 'openalgo-charts'

export interface ResolutionSpec {
  resId: number
  /** What the Fyers historical endpoint calls it, and what our own bars route takes. */
  fyersCode: string
  /** What openalgo-charts calls it. */
  chartInterval: string
  seconds: number
  label: string
}

/** Mirrors the dim_resolution seed rows. Ordered finest first, which is also pill order. */
export const RESOLUTIONS: readonly ResolutionSpec[] = [
  { resId: 1, fyersCode: '5S', chartInterval: '5s', seconds: 5, label: '5 seconds' },
  { resId: 2, fyersCode: '1', chartInterval: '1m', seconds: 60, label: '1 minute' },
  { resId: 3, fyersCode: '2', chartInterval: '2m', seconds: 120, label: '2 minutes' },
  { resId: 4, fyersCode: '3', chartInterval: '3m', seconds: 180, label: '3 minutes' },
  { resId: 5, fyersCode: '5', chartInterval: '5m', seconds: 300, label: '5 minutes' },
  { resId: 6, fyersCode: '10', chartInterval: '10m', seconds: 600, label: '10 minutes' },
  { resId: 7, fyersCode: '15', chartInterval: '15m', seconds: 900, label: '15 minutes' },
  { resId: 8, fyersCode: '20', chartInterval: '20m', seconds: 1200, label: '20 minutes' },
  { resId: 9, fyersCode: '30', chartInterval: '30m', seconds: 1800, label: '30 minutes' },
  { resId: 10, fyersCode: '45', chartInterval: '45m', seconds: 2700, label: '45 minutes' },
  { resId: 11, fyersCode: '60', chartInterval: '1h', seconds: 3600, label: '1 hour' },
  { resId: 12, fyersCode: '120', chartInterval: '2h', seconds: 7200, label: '2 hours' },
  { resId: 13, fyersCode: '180', chartInterval: '3h', seconds: 10800, label: '3 hours' },
  { resId: 14, fyersCode: '240', chartInterval: '4h', seconds: 14400, label: '4 hours' },
  { resId: 100, fyersCode: 'D', chartInterval: '1d', seconds: 86400, label: '1 day' },
]

const BY_FYERS_CODE = new Map(RESOLUTIONS.map((spec) => [spec.fyersCode, spec]))
const BY_CHART_INTERVAL = new Map(RESOLUTIONS.map((spec) => [spec.chartInterval, spec]))
const BY_RES_ID = new Map(RESOLUTIONS.map((spec) => [spec.resId, spec]))

export function specForFyersCode(code: string | null | undefined): ResolutionSpec | null {
  return code ? (BY_FYERS_CODE.get(code) ?? null) : null
}

export function specForChartInterval(interval: string | null | undefined): ResolutionSpec | null {
  return interval ? (BY_CHART_INTERVAL.get(interval) ?? null) : null
}

export function specForResId(resId: number | null | undefined): ResolutionSpec | null {
  return resId === null || resId === undefined ? null : (BY_RES_ID.get(resId) ?? null)
}

/**
 * The Fyers resolution the bars route wants for a chart interval code.
 *
 * Null rather than a fallback. A silent fallback to "1" would download or render a different
 * series than the one the pill says, which is the kind of quiet wrongness this codebase has been
 * bitten by before.
 */
export function fyersCodeForInterval(interval: string): string | null {
  return specForChartInterval(interval)?.fyersCode ?? null
}

export function intervalSeconds(interval: string): number | null {
  return specForChartInterval(interval)?.seconds ?? null
}

/** The bounds row shape the pill list reads. Structural, so both ContractBounds.resolutions and
 *  a Contract.resolutions row satisfy it without a cast. */
export interface ResolutionBoundsLike {
  res_id: number
  fyers_code?: string | null
  chart_interval?: string | null
  rows?: number | null
  first_ts?: number | null
  last_ts?: number | null
}

export type PillState = 'available' | 'no_data' | 'unsupported'

export interface IntervalPill {
  /** The chart interval code. This is what goes to setInterval and into the intervals option. */
  interval: string
  fyersCode: string
  resId: number
  label: string
  seconds: number
  rows: number
  firstTs: number | null
  lastTs: number | null
  state: PillState
  /** Why the pill is not selectable. Rendered next to it rather than hidden. */
  reason: string | null
}

function pillFrom(spec: ResolutionSpec, bound: ResolutionBoundsLike | undefined): IntervalPill {
  const rows = bound?.rows ?? 0
  const firstTs = bound?.first_ts ?? null
  const lastTs = bound?.last_ts ?? null

  // The library's own registry decides, not a pattern match here. A code it refuses would throw
  // UnknownIntervalError inside createWidget and leave the whole screen blank.
  if (!isKnownInterval(spec.chartInterval)) {
    return {
      interval: spec.chartInterval,
      fyersCode: spec.fyersCode,
      resId: spec.resId,
      label: spec.label,
      seconds: spec.seconds,
      rows,
      firstTs,
      lastTs,
      state: 'unsupported',
      reason: 'the chart cannot draw this interval',
    }
  }

  if (rows > 0) {
    return {
      interval: spec.chartInterval,
      fyersCode: spec.fyersCode,
      resId: spec.resId,
      label: spec.label,
      seconds: spec.seconds,
      rows,
      firstTs,
      lastTs,
      state: 'available',
      reason: null,
    }
  }

  return {
    interval: spec.chartInterval,
    fyersCode: spec.fyersCode,
    resId: spec.resId,
    label: spec.label,
    seconds: spec.seconds,
    rows: 0,
    firstTs,
    lastTs,
    state: 'no_data',
    reason: 'not downloaded for this contract',
  }
}

/**
 * The pill list for one contract.
 *
 * `declared` is what the underlying says it downloads (Fyers codes), `bounds` is what the store
 * actually holds. The union is deliberate and is the house rule about controls made concrete: a
 * resolution the user declared but has not downloaded is rendered disabled with the reason
 * visible, not hidden, because hiding it makes a missing download look like a missing feature.
 * A resolution that was downloaded but never declared is still offered, because the data is
 * there and refusing to draw it would be worse.
 *
 * Order is finest first, matching RESOLUTIONS, so the list does not reshuffle as coverage grows.
 */
export function intervalPills(
  declared: readonly string[],
  bounds: readonly ResolutionBoundsLike[],
): IntervalPill[] {
  const byResId = new Map<number, ResolutionBoundsLike>()
  for (const bound of bounds) {
    const existing = byResId.get(bound.res_id)
    // Defensive: one res_id twice would otherwise silently take the last row rather than the one
    // that actually holds rows.
    if (existing === undefined || (bound.rows ?? 0) > (existing.rows ?? 0)) {
      byResId.set(bound.res_id, bound)
    }
  }

  const wanted = new Set<number>()
  for (const code of declared) {
    const spec = specForFyersCode(code)
    if (spec !== null) {
      wanted.add(spec.resId)
    }
  }
  for (const resId of byResId.keys()) {
    wanted.add(resId)
  }

  const pills: IntervalPill[] = []
  for (const spec of RESOLUTIONS) {
    if (!wanted.has(spec.resId)) {
      continue
    }
    pills.push(pillFrom(spec, byResId.get(spec.resId)))
  }
  return pills
}

/** The codes safe to hand createWidget as its `intervals` option: the ones with data behind them
 *  and a code the registry knows. An empty result means the chart has nothing to draw, which the
 *  screen reports rather than mounting an empty terminal. */
export function selectableIntervals(pills: readonly IntervalPill[]): string[] {
  return pills.filter((pill) => pill.state === 'available').map((pill) => pill.interval)
}

/**
 * The interval a contract opens on: the finest one that actually holds rows.
 *
 * Finest rather than largest row count, because the finest series is the one that answers the
 * question a research tool is opened to ask, and the widget only ever pulls lookbackBars of it.
 */
export function defaultInterval(pills: readonly IntervalPill[]): string | null {
  for (const pill of pills) {
    if (pill.state === 'available') {
      return pill.interval
    }
  }
  return null
}

/** Keeps a remembered interval only while the new contract can actually draw it. */
export function resolveInterval(
  pills: readonly IntervalPill[],
  preferred: string | null | undefined,
): string | null {
  if (preferred) {
    const match = pills.find((pill) => pill.interval === preferred && pill.state === 'available')
    if (match !== undefined) {
      return match.interval
    }
  }
  return defaultInterval(pills)
}
