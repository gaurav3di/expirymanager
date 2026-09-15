// A filter that is silently dropped on the way to the wire produces a page of rows that look
// plausible and are not what was asked for. That is the failure this file exists to prevent.

import { describe, expect, it } from 'vitest'

import { EMPTY_FILTERS, contractQueryParams, resolutionSummary } from '@/routes/contracts'
import type { ContractRow } from '@/routes/contracts'

describe('contractQueryParams', () => {
  it('sends only the sort, the direction and the page size when nothing is filtered', () => {
    expect(contractQueryParams(EMPTY_FILTERS, null)).toEqual({
      sort: 'expiry_date',
      dir: 'asc',
      limit: 100,
    })
  })

  it('omits a cleared filter rather than sending an empty string', () => {
    // option_type is validated against a CE or PE pattern, so an empty string is a 422 and not
    // "no opinion".
    const params = contractQueryParams(
      { ...EMPTY_FILTERS, optionType: 'all', kind: 'all', symbolContains: '   ' },
      null,
    )
    expect(params.option_type).toBeUndefined()
    expect(params.kind).toBeUndefined()
    expect(params.symbol_contains).toBeUndefined()
  })

  it('carries every filter the user set', () => {
    const params = contractQueryParams(
      {
        ...EMPTY_FILTERS,
        underlyingId: 3,
        expiryFrom: '2025-01-01',
        expiryTo: '2025-03-31',
        kind: 'OPT',
        optionType: 'CE',
        strikeMin: '23000',
        strikeMax: '24000',
        symbolContains: ' 23000CE ',
        hasData: true,
        sealed: true,
        resId: 2,
        sort: 'strike',
        dir: 'desc',
        limit: 200,
      },
      'opaque-cursor',
    )
    expect(params).toEqual({
      underlying_id: 3,
      expiry_from: '2025-01-01',
      expiry_to: '2025-03-31',
      kind: 'OPT',
      option_type: 'CE',
      strike_min: 23000,
      strike_max: 24000,
      symbol_contains: '23000CE',
      has_data: true,
      sealed: true,
      res_id: 2,
      sort: 'strike',
      dir: 'desc',
      limit: 200,
      cursor: 'opaque-cursor',
    })
  })

  it('sends has_data false as a real filter and not as an absence', () => {
    // Null means no opinion, false means "only contracts with nothing downloaded". Collapsing the
    // two would make the second impossible to ask for.
    expect(contractQueryParams({ ...EMPTY_FILTERS, hasData: false }, null).has_data).toBe(false)
  })

  it('drops a strike box the user has not finished typing in', () => {
    const params = contractQueryParams({ ...EMPTY_FILTERS, strikeMin: '-', strikeMax: '' }, null)
    expect(params.strike_min).toBeUndefined()
    expect(params.strike_max).toBeUndefined()
  })
})

describe('resolutionSummary', () => {
  const row = (resolutions: ContractRow['resolutions']): ContractRow => ({
    contract_id: 1,
    fyers_symbol: 'NSE:NIFTY25MAR2723000CE',
    underlying_id: 1,
    kind: 'OPT',
    instrument_class: 'OPTIDX',
    expiry_date: '2025-03-27',
    strike: 23000,
    option_type: 'CE',
    lot_size: 75,
    sealed_at: null,
    rows: 0,
    resolutions,
  })

  it('names each resolution the contract actually holds, finest first', () => {
    expect(
      resolutionSummary(
        row([
          { res_id: 5, fyers_code: '5', chart_interval: '5m', rows: 2400, first_ts: null, last_ts: null },
          { res_id: 2, fyers_code: '1', chart_interval: '1m', rows: 12000, first_ts: null, last_ts: null },
        ]),
      ),
    ).toEqual(['1m 12k', '5m 2.4k'])
  })

  it('leaves out a resolution row that holds no bars', () => {
    expect(
      resolutionSummary(
        row([
          { res_id: 2, fyers_code: '1', chart_interval: '1m', rows: 0, first_ts: null, last_ts: null },
        ]),
      ),
    ).toEqual([])
  })

  it('falls back to the resolution table when the row carries no chart interval', () => {
    expect(
      resolutionSummary(
        row([
          { res_id: 11, fyers_code: '60', chart_interval: null, rows: 90, first_ts: null, last_ts: null },
        ]),
      ),
    ).toEqual(['1h 90'])
  })
})
