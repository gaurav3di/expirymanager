// The interval table is a copy of a database seed, so the test that matters is the one that
// reads the seed and compares. A silent drift between dim_resolution and this map would download
// one resolution and chart another.

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  RESOLUTIONS,
  defaultInterval,
  fyersCodeForInterval,
  intervalPills,
  intervalSeconds,
  resolveInterval,
  selectableIntervals,
  specForFyersCode,
  specForResId,
} from '@/lib/charts/intervals'

const SCHEMA = path.join(
  import.meta.dirname,
  '../../../../backend/expirymanager/db/duck_schema.sql',
)

/** Parses the dim_resolution seed rows out of the shipped DDL. */
function seededResolutions(): Array<{
  resId: number
  fyersCode: string
  seconds: number
  label: string
  chartInterval: string
}> {
  const sql = fs.readFileSync(SCHEMA, 'utf8')
  const start = sql.indexOf('INSERT INTO dim_resolution')
  expect(start).toBeGreaterThan(-1)
  const block = sql.slice(start, sql.indexOf(';', start))
  const rows: Array<{
    resId: number
    fyersCode: string
    seconds: number
    label: string
    chartInterval: string
  }> = []
  const pattern =
    /\(\s*(\d+)\s*,\s*'([^']+)'\s*,\s*(\d+)\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,/g
  let match = pattern.exec(block)
  while (match !== null) {
    rows.push({
      resId: Number(match[1]),
      fyersCode: match[2],
      seconds: Number(match[3]),
      label: match[4],
      chartInterval: match[5],
    })
    match = pattern.exec(block)
  }
  return rows
}

describe('the resolution table', () => {
  it('is the dim_resolution seed, row for row', () => {
    const seeded = seededResolutions()
    expect(seeded.length).toBeGreaterThan(0)
    expect(RESOLUTIONS.map((spec) => ({ ...spec }))).toEqual(seeded)
  })

  it('maps res_id 2 to the one minute series and not to the Fyers code 1 by accident', () => {
    // res_id 1 is the 5 second series while fyers_code "1" is one minute. Reading one for the
    // other is the mistake this pair exists to make impossible.
    expect(specForResId(1)?.fyersCode).toBe('5S')
    expect(specForFyersCode('1')?.resId).toBe(2)
    expect(fyersCodeForInterval('1m')).toBe('1')
    expect(intervalSeconds('1m')).toBe(60)
  })

  it('refuses an interval it has no Fyers resolution for rather than guessing one', () => {
    expect(fyersCodeForInterval('1w')).toBeNull()
    expect(fyersCodeForInterval('nonsense')).toBeNull()
  })
})

describe('the pill list', () => {
  const downloaded = [
    { res_id: 2, fyers_code: '1', chart_interval: '1m', rows: 12_000 },
    { res_id: 5, fyers_code: '5', chart_interval: '5m', rows: 2_400 },
  ]

  it('offers only what has rows behind it', () => {
    const pills = intervalPills(['1', '5'], downloaded)
    expect(selectableIntervals(pills)).toEqual(['1m', '5m'])
  })

  it('renders a declared but undownloaded resolution disabled with its reason, not hidden', () => {
    const pills = intervalPills(['1', '5', '15'], downloaded)
    const fifteen = pills.find((pill) => pill.interval === '15m')
    expect(fifteen).toBeDefined()
    expect(fifteen?.state).toBe('no_data')
    expect(fifteen?.reason).toBe('not downloaded for this contract')
    expect(fifteen?.rows).toBe(0)
    // And it is not selectable, so a disabled pill can never produce an empty chart.
    expect(selectableIntervals(pills)).not.toContain('15m')
  })

  it('still offers a resolution that was downloaded but never declared', () => {
    const pills = intervalPills([], downloaded)
    expect(selectableIntervals(pills)).toEqual(['1m', '5m'])
  })

  it('orders finest first so the row does not reshuffle as coverage grows', () => {
    const pills = intervalPills(['60', '1', '15'], [
      { res_id: 11, chart_interval: '1h', rows: 90 },
      { res_id: 2, chart_interval: '1m', rows: 5_000 },
    ])
    expect(pills.map((pill) => pill.interval)).toEqual(['1m', '15m', '1h'])
  })

  it('carries the bounds through so the screen can say what it holds', () => {
    const pills = intervalPills([], [
      { res_id: 2, chart_interval: '1m', rows: 7, first_ts: 1_742_953_500, last_ts: 1_742_976_000 },
    ])
    expect(pills[0].firstTs).toBe(1_742_953_500)
    expect(pills[0].lastTs).toBe(1_742_976_000)
  })

  it('keeps one res_id once, taking the row that actually holds bars', () => {
    const pills = intervalPills([], [
      { res_id: 2, chart_interval: '1m', rows: 0 },
      { res_id: 2, chart_interval: '1m', rows: 900 },
    ])
    expect(pills).toHaveLength(1)
    expect(pills[0].rows).toBe(900)
    expect(pills[0].state).toBe('available')
  })
})

describe('choosing the interval', () => {
  const pills = intervalPills(['1', '5', '15'], [
    { res_id: 5, chart_interval: '5m', rows: 2_400 },
    { res_id: 7, chart_interval: '15m', rows: 800 },
  ])

  it('opens on the finest interval that holds rows', () => {
    expect(defaultInterval(pills)).toBe('5m')
  })

  it('keeps a remembered interval only while the new contract can draw it', () => {
    expect(resolveInterval(pills, '15m')).toBe('15m')
    // 1m is declared but empty here, so the remembered choice is dropped rather than carried
    // over into a chart that would answer no bars.
    expect(resolveInterval(pills, '1m')).toBe('5m')
    expect(resolveInterval(pills, null)).toBe('5m')
  })

  it('has nothing to open when nothing was downloaded', () => {
    const empty = intervalPills(['1'], [])
    expect(defaultInterval(empty)).toBeNull()
    expect(selectableIntervals(empty)).toEqual([])
  })
})
