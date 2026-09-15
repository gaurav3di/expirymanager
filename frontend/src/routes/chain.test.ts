// The session scrubber, and the one rule it must never break.
//
// SESSION HOURS ARE NOT CONSTANTS IN THIS DATASET. The NSE derivatives close moved from 15:30 to
// 15:40 on 2026-08-03, measured from live candles, and special sessions have run on a Saturday
// and on a Sunday. So these tests hand the screen timelines that a hardcoded 09:15 to 15:30 model
// would get wrong, and assert it reports what the data says.

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  istClock,
  istDateOf,
  istDayWindow,
  istMidnight,
  nearestIndex,
  sessionSummary,
} from '@/routes/chain'

/** 2025-03-26 09:15:00 IST. */
const OPEN_2025_03_26 = 1_742_960_700

function minutes(from: number, count: number): number[] {
  return Array.from({ length: count }, (_unused, index) => from + index * 60)
}

describe('IST arithmetic', () => {
  it('names the IST calendar date an instant falls on', () => {
    expect(istDateOf(OPEN_2025_03_26)).toBe('2025-03-26')
    // 23:45 IST is still the same Indian day even though it is the next day in UTC.
    expect(istDateOf(istMidnight('2025-03-26') + 23 * 3600 + 45 * 60)).toBe('2025-03-26')
  })

  it('renders the IST wall clock of an instant', () => {
    expect(istClock(OPEN_2025_03_26)).toBe('09:15:00')
  })

  it('bounds a day without claiming anything about when it traded', () => {
    const window = istDayWindow('2025-03-26')
    expect(istClock(window.from)).toBe('00:00:00')
    expect(istClock(window.to)).toBe('23:59:59')
  })
})

describe('the session readout', () => {
  it('reports a 15:29 close from bars that end at 15:29', () => {
    const timeline = minutes(OPEN_2025_03_26, 375)
    expect(sessionSummary(timeline)).toBe('observed session 09:15:00 to 15:29:00 IST, 375 bars')
  })

  it('reports the 15:40 close the exchange moved to, with nothing told about the move', () => {
    // The measured change: NSE derivatives moved their close from 15:30 to 15:40 on 2026-08-03.
    // A screen carrying a constant would still be drawing a scrubber that stopped at 15:29.
    const open = istMidnight('2026-08-03') + 9 * 3600 + 15 * 60
    const timeline = minutes(open, 385)
    expect(sessionSummary(timeline)).toBe('observed session 09:15:00 to 15:39:00 IST, 385 bars')
  })

  it('reports a Sunday special session as readily as a weekday', () => {
    // 2025-02-02 was a Sunday and the exchange traded. A weekday rule would call this no session
    // at all and the scrubber would have refused to move.
    expect(new Date('2025-02-02T00:00:00Z').getUTCDay()).toBe(0)
    const open = istMidnight('2025-02-02') + 9 * 3600 + 15 * 60
    const timeline = minutes(open, 375)
    expect(sessionSummary(timeline)).toContain('observed session 09:15:00 to 15:29:00 IST')
  })

  it('says so plainly when the day holds no bars', () => {
    expect(sessionSummary([])).toBe('no bars on this day')
  })
})

describe('the scrubber', () => {
  const timeline = minutes(OPEN_2025_03_26, 10)

  it('snaps to the stop at or before the instant, never past it', () => {
    expect(nearestIndex(timeline, OPEN_2025_03_26)).toBe(0)
    expect(nearestIndex(timeline, OPEN_2025_03_26 + 59)).toBe(0)
    expect(nearestIndex(timeline, OPEN_2025_03_26 + 60)).toBe(1)
    expect(nearestIndex(timeline, OPEN_2025_03_26 + 5 * 60 + 30)).toBe(5)
  })

  it('holds at the last stop for an instant past the end of the day', () => {
    expect(nearestIndex(timeline, OPEN_2025_03_26 + 86_400)).toBe(9)
  })

  it('holds at the first stop for an instant before the day opened', () => {
    expect(nearestIndex(timeline, OPEN_2025_03_26 - 3600)).toBe(0)
  })

  it('has an answer for an empty timeline', () => {
    expect(nearestIndex([], OPEN_2025_03_26)).toBe(0)
  })
})

describe('the chain screen source', () => {
  const source = fs.readFileSync(path.join(import.meta.dirname, 'chain.tsx'), 'utf8')
  // Comments explain the rule and therefore quote the forbidden times. The code is what is
  // checked.
  const code = source.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')

  it('names no market open, no market close and no session length', () => {
    for (const forbidden of ['09:15', '15:30', '15:40', '15:29', '09:00', '375']) {
      expect(code).not.toContain(forbidden)
    }
  })

  it('finds the neighbouring trading day by asking for a bar, not by counting weekdays', () => {
    // A holiday, a weekend and a special Sunday session are all the same question here: which
    // bar exists next. Nothing in the file may reason about weekdays.
    expect(code).toContain('/bars/before')
    expect(code).not.toContain('getDay')
    expect(code).not.toContain('getUTCDay')
  })
})
