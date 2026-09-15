import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { chainTotals } from '@/components/chain/ChainGrid'
import type { ChainGridRow } from '@/components/chain/ChainGrid'

function leg(close: number | null, volume: number | null, oi: number | null) {
  return { contract_id: 1, fyers_symbol: 'NSE:X', open: null, high: null, low: null, close, volume, oi }
}

const ROWS: ChainGridRow[] = [
  { strike: 22_900, lot_size: 75, is_atm: false, ce: leg(120, 10, 1000), pe: leg(40, 5, 500) },
  { strike: 23_000, lot_size: 75, is_atm: true, ce: leg(80, 20, 3000), pe: leg(70, 30, 6000) },
  { strike: 23_100, lot_size: 75, is_atm: false, ce: leg(null, null, null), pe: null },
]

describe('chainTotals', () => {
  it('adds only the legs that carry a figure', () => {
    const totals = chainTotals(ROWS)
    expect(totals.callOi).toBe(4000)
    expect(totals.putOi).toBe(6500)
    expect(totals.callVolume).toBe(30)
    expect(totals.putVolume).toBe(35)
  })

  it('reports the put call ratio from open interest', () => {
    expect(chainTotals(ROWS).pcr).toBeCloseTo(6500 / 4000, 10)
  })

  it('has no ratio rather than a zero when no call holds open interest', () => {
    // 0.00 would read as a real measurement of a very call heavy chain, which is the opposite of
    // what an absent denominator means.
    const totals = chainTotals([
      { strike: 1, lot_size: null, is_atm: false, ce: null, pe: leg(1, 1, 100) },
    ])
    expect(totals.pcr).toBeNull()
  })
})

describe('the grid source', () => {
  const source = fs.readFileSync(path.join(import.meta.dirname, 'ChainGrid.tsx'), 'utf8')

  it('highlights the at the money row from the flag and never from a float comparison', () => {
    // Strikes cross the wire as JSON doubles. Deriving the ATM row by comparing strike with
    // atm_strike is a float equality test that highlights the wrong row silently when it drifts.
    expect(source).toContain('row.is_atm')
    expect(source).not.toContain('row.strike === atmStrike')
    expect(source).not.toContain('strike === atm')
  })
})
