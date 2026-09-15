// The download flow, tested where its decisions live.
//
// Four of these assert a thing this codebase has got wrong before, or that the product cannot
// afford to get wrong once:
//
//  1. A selection made on one cursor page survives paging and a select all on the next page.
//  2. Start is DISABLED with a reason when the plan does not fit today's budget, and the number
//     carried back on submit is the number that was shown.
//  3. The at the money band is refused, not quietly widened, when the underlying history does
//     not reach the selected expiry day.
//  4. The spot history remedy plans spot bars and nothing else.
//
// Nothing here is a credential and nothing here talks to a backend.

import { describe, expect, it } from 'vitest'

import { ApiError } from '@/lib/api/client'
import {
  buildPlanRequest,
  buildSpotHistoryRequest,
  defaultSheetValue,
  parseStrikes,
  resolutionChoices,
  sameRequest,
  sheetProblems,
  spotAvailability,
  spotResolutions,
  SPOT_SHEET_PLACEHOLDER_DATE,
  type DownloadSheetValue,
  type SheetUnderlying,
} from '@/components/download/DownloadSheet'
import {
  contractsText,
  expiryCoverageBarProps,
  expiryRowId,
  reconcileSelection,
  rowSelectionStateOf,
  selectedRows,
  strikeRangeText,
  type ExpiryRow,
} from '@/components/download/ExpirySelectTable'
import { summariseSelection, summaryLine } from '@/components/download/SelectionBar'
import {
  describePlanFailure,
  startDecision,
  type PlanPreviewBody,
} from '@/components/download/PlanPreview'

// ---------------------------------------------------------------------------
// Fixtures, shaped exactly like the wire
// ---------------------------------------------------------------------------

function expiry(overrides: Partial<ExpiryRow> & { expiry_date: string }): ExpiryRow {
  return {
    expiry_id: 1,
    expiry_dow: 3,
    has_futures: true,
    has_options: true,
    futures_count: 3,
    options_count: 428,
    contract_count: 431,
    expiry_cycle: 'M',
    expiry_cycle_source: 'derived',
    is_last_of_month: true,
    discovered_at: '2025-03-01T10:00:00',
    contracts_discovered_at: '2025-03-28T18:15:04',
    contract_id_lo: 41984,
    contract_id_hi: 42414,
    min_strike: 18000,
    max_strike: 28000,
    strike_step: 50,
    coverage: {
      contracts_with_data: 412,
      contracts_without_data: 19,
      contracts_sealed: 431,
      chunks_ok: 431,
      chunks_empty: 4,
      chunks_error: 0,
      rows: 1284900,
    },
    ...overrides,
  }
}

function preview(overrides: Partial<PlanPreviewBody> = {}): PlanPreviewBody {
  return {
    tasks_total: 4820,
    requests_estimated: 4820,
    chunks_skipped_covered: 120,
    contracts_sealed_skipped: 0,
    rows_estimated: 1_284_900,
    bytes_estimated: 45_000_000,
    eta_seconds: 1702,
    budget_used_today: 1200,
    budget_remaining_today: 98_800,
    budget_after: 93_980,
    warnings: [],
    expiries_planned: 8,
    contracts_planned: 3448,
    discovery_tasks: 0,
    estimated_downstream_requests: 0,
    spot_tasks: 0,
    budget_allowance: 98_800,
    reserve_applied: false,
    exceeds_budget: false,
    warning_details: [],
    ...overrides,
  }
}

const underlying: SheetUnderlying = {
  underlying_id: 1,
  fyers_symbol: 'NSE:NIFTY50-INDEX',
  display_name: 'Nifty 50',
  exchange: 'NSE',
  data_from: '2022-01-03',
  default_resolutions: ['1', '5'],
  include_oi: true,
  spot_bars: 512340,
  spot_last_ts: '2026-09-08T15:29:00',
}

// ---------------------------------------------------------------------------
// Selection across cursor pages
// ---------------------------------------------------------------------------

describe('selection across cursor pages', () => {
  const pageOne = [expiry({ expiry_date: '2025-03-06' }), expiry({ expiry_date: '2025-03-13' })]
  const pageTwo = [expiry({ expiry_date: '2025-03-20' }), expiry({ expiry_date: '2025-03-27' })]

  it('keeps page one ticked when page two selects everything it can see', () => {
    // The table's own state only ever describes the page it holds. Taking it literally is how a
    // four expiry selection silently becomes a two expiry download.
    const afterPageOne = reconcileSelection({}, pageOne, { '2025-03-06': true })
    expect(Object.keys(afterPageOne)).toEqual(['2025-03-06'])

    const afterPageTwo = reconcileSelection(afterPageOne, pageTwo, {
      '2025-03-20': true,
      '2025-03-27': true,
    })
    expect(Object.keys(afterPageTwo).sort()).toEqual(['2025-03-06', '2025-03-20', '2025-03-27'])
  })

  it('unticks only rows the user could actually see', () => {
    const selection = reconcileSelection({}, [...pageOne, ...pageTwo], {
      '2025-03-06': true,
      '2025-03-20': true,
    })
    // Clearing the page two checkbox must not touch the page one entry.
    const cleared = reconcileSelection(selection, pageTwo, { '2025-03-20': false })
    expect(Object.keys(cleared)).toEqual(['2025-03-06'])
  })

  it('keeps the off page keys in the table state so the header checkbox is not lying', () => {
    const selection = reconcileSelection({}, pageOne, { '2025-03-06': true })
    expect(rowSelectionStateOf(selection)).toEqual({ '2025-03-06': true })
  })

  it('hands the selection back in expiry date order, which is what the plan wants', () => {
    const selection = reconcileSelection({}, [...pageTwo, ...pageOne], {
      '2025-03-27': true,
      '2025-03-06': true,
    })
    expect(selectedRows(selection).map(expiryRowId)).toEqual(['2025-03-06', '2025-03-27'])
  })
})

// ---------------------------------------------------------------------------
// What a row renders
// ---------------------------------------------------------------------------

describe('expiry row values', () => {
  it('draws the coverage bar in contracts, because that is what the ledger can answer', () => {
    const props = expiryCoverageBarProps(expiry({ expiry_date: '2025-03-27' }))
    expect(props.ok).toBe(412)
    expect(props.missing).toBe(19)
    // Empty is left at zero deliberately: candle_coverage counts empty WINDOWS, and turning a
    // window count into a contract count would be a number nobody wrote.
    expect(props.empty).toBe(0)
    expect(props.rows).toBe(1284900)
  })

  it('renders an undiscovered expiry as nothing rather than as fully held', () => {
    const row = expiry({
      expiry_date: '2026-09-30',
      contracts_discovered_at: null,
      contract_count: 0,
      futures_count: 0,
      options_count: 0,
      coverage: {
        contracts_with_data: 0,
        contracts_without_data: 0,
        contracts_sealed: 0,
        chunks_ok: 0,
        chunks_empty: 0,
        chunks_error: 0,
        rows: 0,
      },
    })
    const props = expiryCoverageBarProps(row)
    expect(props.ok + props.empty + props.missing).toBe(0)
    expect(contractsText(row)).toBe('not discovered')
  })

  it('states the strike geometry the strike scope will be applied to', () => {
    expect(strikeRangeText(expiry({ expiry_date: '2025-03-27' }))).toBe(
      '18,000 to 28,000 step 50',
    )
  })
})

// ---------------------------------------------------------------------------
// The sticky summary
// ---------------------------------------------------------------------------

describe('selection summary', () => {
  it('sums the selection and names the expiries that still need discovery', () => {
    const rows = [
      expiry({ expiry_date: '2025-03-06' }),
      expiry({
        expiry_date: '2025-03-13',
        contracts_discovered_at: null,
        contract_count: 0,
        futures_count: 0,
        options_count: 0,
        coverage: {
          contracts_with_data: 0,
          contracts_without_data: 0,
          contracts_sealed: 0,
          chunks_ok: 0,
          chunks_empty: 0,
          chunks_error: 0,
          rows: 0,
        },
      }),
    ]
    const summary = summariseSelection(rows)
    expect(summary.expiries).toBe(2)
    expect(summary.contracts).toBe(431)
    expect(summary.contractsWithData).toBe(412)
    expect(summary.undiscovered).toBe(1)
    expect(summary.firstExpiry).toBe('2025-03-06')
    expect(summary.lastExpiry).toBe('2025-03-13')
    expect(summaryLine(summary)).toBe(
      '2025-03-06 to 2025-03-13. 431 contracts, 412 already hold data. 1 expiry need contract discovery first',
    )
  })
})

// ---------------------------------------------------------------------------
// The gate on Start
// ---------------------------------------------------------------------------

describe('startDecision', () => {
  it('carries back exactly the request count that was shown', () => {
    const decision = startDecision(preview({ requests_estimated: 4820 }))
    expect(decision.canStart).toBe(true)
    expect(decision.confirmRequests).toBe(4820)
    expect(decision.reason).toBeNull()
  })

  it('disables Start with an inline reason when the plan exceeds the remaining budget', () => {
    const decision = startDecision(
      preview({
        requests_estimated: 62_480,
        budget_allowance: 18_204,
        budget_after: -44_276,
        exceeds_budget: true,
      }),
    )
    expect(decision.canStart).toBe(false)
    expect(decision.offerDefer).toBe(true)
    expect(decision.reason).toBe(
      "This plan needs 62,480 requests and 18,204 remain in today's budget. Queue it for " +
        'tomorrow, or narrow the selection.',
    )
  })

  it('lets the same plan through once it is queued for tomorrow', () => {
    const over = preview({ requests_estimated: 62_480, budget_allowance: 18_204, exceeds_budget: true })
    const decision = startDecision(over, { deferToTomorrow: true })
    expect(decision.canStart).toBe(true)
    expect(decision.confirmRequests).toBe(62_480)
  })

  it('says the sweep reserve was the binding constraint when it was', () => {
    const decision = startDecision(
      preview({ requests_estimated: 900, budget_allowance: 400, exceeds_budget: true, reserve_applied: true }),
    )
    expect(decision.reason).toContain('remain inside the sweep reserve today')
  })

  it('refuses a sheet that asks for nothing new rather than queueing an empty job', () => {
    const decision = startDecision(preview({ tasks_total: 0, requests_estimated: 0 }))
    expect(decision.canStart).toBe(false)
    expect(decision.reason).toContain('already recorded in the coverage ledger')
  })

  it('will not start before a price exists', () => {
    expect(startDecision(null).canStart).toBe(false)
    expect(startDecision(null).confirmRequests).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Failures with a next step
// ---------------------------------------------------------------------------

describe('describePlanFailure', () => {
  it('turns a missing contract catalog into a discovery remedy', () => {
    const failure = describePlanFailure(
      new ApiError({
        status: 400,
        code: 'no_contracts_discovered',
        message: 'No contracts have been discovered for the selected expiries yet.',
      }),
    )
    expect(failure.remedy).toBe('discover_contracts')
    expect(failure.repriceable).toBe(false)
  })

  it('turns a missing spot series into the underlying history remedy', () => {
    const failure = describePlanFailure(
      new ApiError({
        status: 400,
        code: 'strike_scope_needs_spot',
        message: 'An at the money band needs the underlying history.',
      }),
    )
    expect(failure.remedy).toBe('spot_history')
  })

  it('marks a stale preview as something to re-price rather than retry', () => {
    const failure = describePlanFailure(
      new ApiError({ status: 409, code: 'plan_changed', message: 'The estimate no longer matches.' }),
    )
    expect(failure.repriceable).toBe(true)
    expect(failure.message).toContain('The catalog changed between the price and the start')
  })

  it('keeps the backend sentence for anything it does not have a remedy for', () => {
    const failure = describePlanFailure(
      new ApiError({ status: 503, code: 'pipeline_stopped', message: 'The pipeline is stopped.' }),
    )
    expect(failure.message).toBe('The pipeline is stopped.')
    expect(failure.remedy).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The sheet
// ---------------------------------------------------------------------------

describe('the download sheet', () => {
  it('starts from the resolutions and open interest the underlying was registered with', () => {
    const value = defaultSheetValue(underlying)
    expect(value.resolutions).toEqual(['1', '5'])
    expect(value.includeOi).toBe(true)
    expect(value.strikeScope).toEqual({ mode: 'all' })
  })

  it('offers a resolution the backend knows even when this build has never heard of it', () => {
    const codes = resolutionChoices(['1', '10S']).map((choice) => choice.code)
    expect(codes).toContain('10S')
    expect(codes.filter((code) => code === '1')).toHaveLength(1)
    expect(resolutionChoices(['10S']).find((choice) => choice.code === '10S')?.isSeconds).toBe(true)
  })

  it('names the problems the backend would answer 422 for', () => {
    const value: DownloadSheetValue = {
      resolutions: [],
      instrumentClass: 'OPT',
      optionTypes: [],
      strikeScope: { mode: 'explicit', strikes: [] },
      includeOi: true,
      forceRefresh: false,
    }
    expect(sheetProblems(value)).toEqual([
      'Choose at least one resolution.',
      'Choose calls, puts or both.',
      'List at least one strike, or switch back to every strike.',
    ])
  })

  it('reads a pasted strike list however it was separated', () => {
    expect(parseStrikes('23000, 23100\n23200 23000')).toEqual([23000, 23100, 23200])
    expect(parseStrikes('   ')).toEqual([])
  })

  it('builds the plan body the route documents', () => {
    const body = buildPlanRequest(defaultSheetValue(underlying), {
      underlyingId: 1,
      expiryDates: ['2025-03-06', '2025-03-13'],
    })
    expect(body).toEqual({
      underlying_id: 1,
      expiry_dates: ['2025-03-06', '2025-03-13'],
      resolutions: ['1', '5'],
      instrument_class: 'OPT',
      option_types: ['CE', 'PE'],
      strike_scope: { mode: 'all' },
      include_oi: true,
      force_refresh: false,
      range_from: null,
      range_to: null,
      include_spot: false,
    })
  })

  it('does not re-price a request it has already priced', () => {
    const first = buildPlanRequest(defaultSheetValue(underlying), {
      underlyingId: 1,
      expiryDates: ['2025-03-06'],
    })
    const second = buildPlanRequest(defaultSheetValue(underlying), {
      underlyingId: 1,
      expiryDates: ['2025-03-06'],
    })
    expect(sameRequest(first, second)).toBe(true)
    expect(sameRequest(null, second)).toBe(false)
    expect(
      sameRequest(first, { ...second, force_refresh: true }),
    ).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The at the money band and its remedy
// ---------------------------------------------------------------------------

describe('spotAvailability', () => {
  it('allows the band when the stored history reaches every selected expiry', () => {
    const availability = spotAvailability(underlying, ['2025-03-06', '2025-03-27'])
    expect(availability.available).toBe(true)
    expect(availability.reason).toBeNull()
    expect(availability.lastSpotDay).toBe('2026-09-08')
  })

  it('refuses the band, with the reason, when no spot bars are stored at all', () => {
    const availability = spotAvailability(
      { ...underlying, spot_bars: 0, spot_last_ts: null },
      ['2025-03-06'],
    )
    expect(availability.available).toBe(false)
    expect(availability.reason).toBe(
      'No bars are stored for NSE:NIFTY50-INDEX itself, so the at the money strike for an ' +
        'expiry day cannot be found.',
    )
  })

  it('refuses the band when the history stops before a selected expiry day', () => {
    const availability = spotAvailability(
      { ...underlying, spot_last_ts: '2025-03-10T15:29:00' },
      ['2025-03-06', '2025-03-13', '2025-03-27'],
    )
    expect(availability.available).toBe(false)
    expect(availability.reason).toBe(
      'The stored history for NSE:NIFTY50-INDEX ends on 2025-03-10, and 2 selected expiries ' +
        'fall after it.',
    )
  })
})

describe('the underlying history remedy', () => {
  const value = defaultSheetValue(underlying)

  it('plans spot bars over exactly the span of the selection, and no contracts', () => {
    const body = buildSpotHistoryRequest(value, underlying, ['2025-03-27', '2025-03-06'])
    expect(body.include_spot).toBe(true)
    expect(body.range_from).toBe('2025-03-06')
    expect(body.range_to).toBe('2025-03-27')
    // The placeholder is what keeps every real expiry's contracts out of this plan: the planner
    // only prices contracts for expiry dates that are in the catalog, and this one never is.
    expect(body.expiry_dates).toEqual([SPOT_SHEET_PLACEHOLDER_DATE])
    expect(SPOT_SHEET_PLACEHOLDER_DATE < '2022-01-03').toBe(true)
    expect(body.include_oi).toBe(false)
  })

  it('drops second resolutions, whose window is the last 30 trading days and not a backfill', () => {
    expect(spotResolutions({ ...value, resolutions: ['5S', '1'] }, underlying)).toEqual(['1'])
    expect(spotResolutions({ ...value, resolutions: ['5S'] }, underlying)).toEqual(['1', '5'])
    expect(
      spotResolutions({ ...value, resolutions: [] }, { ...underlying, default_resolutions: [] }),
    ).toEqual(['1'])
  })
})
