// Open interest is the one series on this screen that has no home on Bar, so it goes through the
// Tier 2 contract. The registration is the part worth asserting: a descriptor that is written,
// exported and never registered is a picker entry that is not there and an addIndicator that
// throws, with nothing in any log to say so.

import { describe, expect, it } from 'vitest'
import { hasIndicator, registeredIndicators } from 'openalgo-charts'

import {
  OPEN_INTEREST_ID,
  OPEN_INTEREST_PLOT,
  buildOpenInterestIndicator,
  fetchOpenInterest,
  registerOpenInterestIndicator,
} from '@/components/charts/oiIndicator'
import type { OpenInterestPoint, OpenInterestSource } from '@/components/charts/oiIndicator'

interface Call {
  symbol: string
  interval: string
  from?: number
  to?: number
}

function source(byWindow: OpenInterestPoint[], wholeLife: OpenInterestPoint[]) {
  const calls: Call[] = []
  const impl: OpenInterestSource = {
    openInterest: async (symbol, interval, from, to) => {
      calls.push({ symbol, interval, from, to })
      return from === undefined ? wholeLife : byWindow
    },
  }
  return { calls, impl }
}

describe('fetchOpenInterest', () => {
  it('reads the window the chart is showing and keys the values by the plot', async () => {
    const { calls, impl } = source([{ time: 100, oi: 5000 }], [])
    const points = await fetchOpenInterest(impl, { symbol: 'NSE:X', interval: '1m' }, 100, 200)

    expect(calls).toEqual([{ symbol: 'NSE:X', interval: '1m', from: 100, to: 200 }])
    expect(points).toEqual([{ time: 100, values: { [OPEN_INTEREST_PLOT]: 5000 } }])
  })

  it('falls back to the whole life when the window came back empty', async () => {
    // The runtime hands fetch the first and last time of the bars the chart currently holds. A
    // contract change can be applied before the new bars land, in which case that window belongs
    // to the contract the user just left. One extra request beats an empty pane under visible
    // candles, which reads as "open interest was never downloaded".
    const { calls, impl } = source([], [{ time: 42, oi: 7 }])
    const points = await fetchOpenInterest(impl, { symbol: 'NSE:X', interval: '1m' }, 100, 200)

    expect(calls).toHaveLength(2)
    expect(calls[1].from).toBeUndefined()
    expect(points).toEqual([{ time: 42, values: { oi: 7 } }])
  })

  it('does not send a window the chart could not supply', async () => {
    const { calls, impl } = source([], [{ time: 1, oi: 2 }])
    await fetchOpenInterest(impl, { symbol: 'NSE:X', interval: '1m' }, 0, 0)
    expect(calls).toHaveLength(1)
    expect(calls[0].from).toBeUndefined()
  })
})

describe('the descriptor', () => {
  it('refetches on both identity keys, so the pane follows the contract', () => {
    const descriptor = buildOpenInterestIndicator({ openInterest: async () => [] })
    expect(descriptor.id).toBe(OPEN_INTEREST_ID)
    expect(descriptor.placement).toBe('pane')
    expect(descriptor.inputs.map((input) => input.key)).toEqual(['symbol', 'interval'])
    expect(descriptor.plots.map((plot) => plot.key)).toEqual([OPEN_INTEREST_PLOT])
  })

  it('lands in the registry when registered, which is what the picker reads', () => {
    registerOpenInterestIndicator({ openInterest: async () => [] })

    expect(hasIndicator(OPEN_INTEREST_ID)).toBe(true)
    const listed = registeredIndicators().filter((entry) => entry.id === OPEN_INTEREST_ID)
    expect(listed).toHaveLength(1)
    expect(listed[0].name).toBe('Open interest')

    // Registering again is a no-op rather than a second entry in the picker.
    registerOpenInterestIndicator({ openInterest: async () => [] })
    expect(registeredIndicators().filter((entry) => entry.id === OPEN_INTEREST_ID)).toHaveLength(1)
  })

  it('imports the indicators tier, so the picker is not empty', () => {
    // A bare `import 'openalgo-charts/indicators'` is what registers the 102 built-ins. Without
    // it the Indicators button opens onto nothing at all.
    expect(registeredIndicators().length).toBeGreaterThan(50)
  })
})
