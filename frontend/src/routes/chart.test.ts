import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { exchangeOf, istTextFromEpoch } from '@/routes/chart'
import { formatDateTime } from '@/lib/format'

describe('exchangeOf', () => {
  it('reads the exchange the Fyers symbol already carries', () => {
    expect(exchangeOf('NSE:NIFTY25MAR2723000CE')).toBe('NSE')
    expect(exchangeOf('BSE:SENSEX25MAR81000PE')).toBe('BSE')
  })

  it('has nothing to report for a symbol with no prefix', () => {
    expect(exchangeOf('NIFTY')).toBe('')
  })
})

describe('istTextFromEpoch', () => {
  it('renders a bounds timestamp as the IST wall clock the rest of the app shows', () => {
    // The bounds route emits UTC seconds while every other catalogue timestamp is ISO text, so
    // this is the one conversion, and it has to land on the same minute the tables print.
    expect(formatDateTime(istTextFromEpoch(1_742_960_700))).toBe('26 Mar 2025, 09:15:00 IST')
  })

  it('has no text for a resolution with no bars', () => {
    expect(istTextFromEpoch(null)).toBeNull()
  })
})

describe('the chart screen source', () => {
  const source = fs.readFileSync(path.join(import.meta.dirname, 'chart.tsx'), 'utf8')
  const code = source.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')

  it('primes the feed with the bounds it already fetched, rather than leaving it to discover them', () => {
    // The bounds query behind the interval pills is the same fact the clamp needs. Fetching it
    // and not pushing it into the feed is the "written, exported, never called" failure: correct
    // code, green tests, and a first load that spends a round trip discovering the obvious.
    expect(code).toContain('expiryFeed.primeBounds(')
    expect(code).toContain('expiryFeed.registerContract(')
  })

  it('withholds the symbol from the chart until the feed has been primed for it', () => {
    expect(code).toContain('primedFor === symbol')
  })

  it('offers the widget only the intervals that have bars behind them', () => {
    expect(code).toContain('selectableIntervals(pills)')
    expect(code).toContain('intervals={selectable}')
  })

  it('does not key the terminal on the symbol or the interval', () => {
    // Keying on either would rebuild the widget on every contract change, which is exactly what
    // setSymbol and setInterval exist to avoid.
    expect(code).toContain('key={selectable.join(')
    expect(code).not.toContain('key={chartSymbol}')
    expect(code).not.toContain('key={chartInterval}')
  })
})
