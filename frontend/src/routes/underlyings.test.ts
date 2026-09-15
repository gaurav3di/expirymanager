// The universe screen and the add-your-own flow.
//
// Two of these guard measured facts rather than preferences:
//
//  - MCX is refused before a request is spent. Seven MCX forms answered HTTP 422 on 2026-09-09
//    while BSE:SENSEX-INDEX answered 200 in the same run, so the expired endpoints do not serve
//    it and searching for it would spend budget to be told so.
//  - An underlying with no DuckDB mirror is reported on the row. That state has occurred here:
//    four seeded registry rows had no mirror and nine joins answered empty with no error.
//
// Nothing here is a credential.

import { describe, expect, it } from 'vitest'

import { ApiError } from '@/lib/api/client'
import {
  MCX_NOT_SERVED,
  describeCatalogFailure,
  guardQuery,
  probeLine,
  type ResolveProbeWire,
} from '@/components/underlyings/AddUnderlyingDialog'
import type { UnderlyingWire } from '@/components/underlyings/AddUnderlyingDialog'
import { heldLine, spotLine, underlyingStatusBadges } from '@/routes/underlyings'
import {
  contractDiscoverySchedule,
  describeDiscoverAccepted,
  discoverDefaults,
  istToday,
  selectionFor,
} from '@/routes/expiries'
import type { ExpiryRow } from '@/components/download/ExpirySelectTable'

function underlying(overrides: Partial<UnderlyingWire> = {}): UnderlyingWire {
  return {
    underlying_id: 1,
    fyers_symbol: 'NSE:NIFTY50-INDEX',
    root: 'NIFTY',
    exchange: 'NSE',
    segment: 'CM',
    instrument_kind: 'INDEX',
    display_name: 'Nifty 50',
    data_from: '2022-01-03',
    default_resolutions: ['1', '5'],
    include_oi: true,
    option_life_days: 200,
    future_life_days: 400,
    spot_contract_id: 1,
    resolved_root_echo: 'NIFTY',
    resolved_at: '2026-09-09T11:04:00',
    is_builtin: true,
    is_active: true,
    mirrored: true,
    expiry_count: 214,
    contract_count: 61240,
    first_expiry: '2022-01-06',
    last_expiry: '2026-09-04',
    spot_bars: 512340,
    spot_last_ts: '2026-09-08T15:29:00',
    ...overrides,
  }
}

describe('guardQuery', () => {
  it('refuses an MCX search before a governed request is spent', () => {
    expect(guardQuery('MCX:CRUDEOIL-COM').ok).toBe(false)
    expect(guardQuery('MCX:CRUDEOIL-COM').message).toBe(MCX_NOT_SERVED)
    expect(guardQuery('mcx').ok).toBe(false)
  })

  it('says why in words that name the measurement, not a rule of thumb', () => {
    expect(MCX_NOT_SERVED).toContain('2026-09-09')
    expect(MCX_NOT_SERVED).toContain('422')
    expect(MCX_NOT_SERVED).toContain('NSE or a BSE')
  })

  it('allows an NSE or BSE search', () => {
    expect(guardQuery('NIFTYNXT50').ok).toBe(true)
    expect(guardQuery('BSE:SENSEX-INDEX').ok).toBe(true)
  })

  it('refuses a query longer than the backend accepts, without a round trip', () => {
    expect(guardQuery('N'.repeat(65)).ok).toBe(false)
    expect(guardQuery('N'.repeat(64)).ok).toBe(true)
  })

  it('is quietly not ready for an empty box rather than shouting at it', () => {
    expect(guardQuery('  ').ok).toBe(false)
    expect(guardQuery('  ').message).toBeNull()
  })
})

describe('probeLine', () => {
  function probe(overrides: Partial<ResolveProbeWire> = {}): ResolveProbeWire {
    return {
      attempted: true,
      symbol: 'NSE:NIFTYNXT50-INDEX',
      root_echo: 'NIFTYNXT50',
      expiry_count: 12,
      range_from: '2025-09-15',
      range_to: '2026-09-15',
      reason: null,
      ...overrides,
    }
  }

  it('quotes the vendor echo, which is the authoritative root', () => {
    expect(probeLine(probe())).toBe(
      'Fyers echoed NIFTYNXT50 for NSE:NIFTYNXT50-INDEX, 12 expiries over 2025-09-15 to 2026-09-15.',
    )
  })

  it('says the root was not confirmed when the token was gone, rather than implying it was', () => {
    const line = probeLine(probe({ attempted: false, reason: 'needs_reauth' }))
    expect(line).toContain('was not asked to confirm')
    expect(line).toContain('Fyers session has expired')
  })
})

describe('describeCatalogFailure', () => {
  it('adds the wait to a rate limited refusal', () => {
    const message = describeCatalogFailure(
      new ApiError({
        status: 429,
        code: 'rate_limited',
        message: 'Too many searches.',
        retryAfterSeconds: 45,
      }),
    )
    expect(message).toBe('Too many searches. Try again in 45 seconds.')
  })

  it('shows the backend sentence unchanged for everything else', () => {
    expect(
      describeCatalogFailure(
        new ApiError({ status: 409, code: 'already_exists', message: 'That root is registered.' }),
      ),
    ).toBe('That root is registered.')
  })
})

describe('underlying row', () => {
  it('reports a missing DuckDB mirror on the row, first', () => {
    const badges = underlyingStatusBadges(underlying({ mirrored: false }))
    expect(badges[0]?.label).toBe('mirror missing')
    expect(badges[0]?.tone).toBe('warning')
    expect(badges[0]?.hint).toContain('answers empty everywhere')
  })

  it('carries no warning badge for a healthy builtin', () => {
    const badges = underlyingStatusBadges(underlying())
    expect(badges.map((badge) => badge.label)).toEqual(['builtin'])
  })

  it('says a root was never probed rather than claiming it was confirmed', () => {
    const badges = underlyingStatusBadges(underlying({ resolved_root_echo: null }))
    expect(badges.map((badge) => badge.label)).toContain('root unconfirmed')
  })

  it('renders what is held, not what was asked for', () => {
    expect(heldLine(underlying())).toBe(
      '214 expiries, 61,240 contracts, 2022-01-06 to 2026-09-04',
    )
    expect(heldLine(underlying({ expiry_count: 0 }))).toBe('No expiries discovered yet')
  })

  it('names the spot history the at the money band depends on', () => {
    expect(spotLine(underlying())).toBe('512k bars to 2026-09-08')
    expect(spotLine(underlying({ spot_bars: 0, spot_last_ts: null }))).toBe('no bars')
  })
})

describe('expiry discovery', () => {
  it('starts from the registry floor and today, and lets the backend clamp the far end', () => {
    expect(discoverDefaults({ data_from: '2022-01-03' }, '2026-09-15')).toEqual({
      range_from: '2022-01-03',
      range_to: '2026-09-15',
    })
  })

  it('reads today as an IST calendar date regardless of the machine zone', () => {
    // 2026-09-15T20:30:00Z is already 2026-09-16 in IST. A browser in UTC must still agree with
    // the exchange's calendar day.
    expect(istToday(new Date('2026-09-15T20:30:00Z'))).toBe('2026-09-16')
    expect(istToday(new Date('2026-09-15T10:00:00Z'))).toBe('2026-09-15')
  })

  it('says how many requests were queued and whether the range was clamped', () => {
    expect(
      describeDiscoverAccepted({
        job_id: 'j1',
        kind: 'expiry_discovery',
        status: 'queued',
        total_tasks: 2,
        est_requests: 2,
        range_from: '2022-01-03',
        range_to: '2026-09-12',
        clamped: true,
        windows: [
          { range_from: '2022-01-03', range_to: '2023-01-03' },
          { range_from: '2023-01-04', range_to: '2026-09-12' },
        ],
      }),
    ).toBe(
      '2 windows queued over 2022-01-03 to 2026-09-12, clamped to the last day the vendor serves. One request each.',
    )
  })
})

describe('contractDiscoverySchedule', () => {
  const rows = [
    { schedule_id: 'a', name: 'Nightly backfill', kind: 'rolling_backfill', enabled: true, is_builtin: true },
    { schedule_id: 'b', name: 'Mine', kind: 'contract_discovery', enabled: true, is_builtin: false },
    { schedule_id: 'c', name: 'Contract discovery', kind: 'contract_discovery', enabled: true, is_builtin: true },
  ]

  it('finds the discovery schedule by kind and prefers the seeded one', () => {
    expect(contractDiscoverySchedule(rows)?.schedule_id).toBe('c')
  })

  it('answers null rather than guessing an id when none is installed', () => {
    expect(contractDiscoverySchedule([rows[0]])).toBeNull()
    expect(contractDiscoverySchedule(undefined)).toBeNull()
  })
})

describe('selectionFor', () => {
  // An expiry date carries no underlying with it, and the plan request carries dates. So a
  // selection that belongs to another underlying has to disappear when the screen switches,
  // rather than be priced against the wrong chain.
  const row = { expiry_date: '2025-03-27' } as ExpiryRow
  const owned = { underlyingId: 1, selection: { '2025-03-27': row } }

  it('hands back the selection for the underlying that made it', () => {
    expect(Object.keys(selectionFor(owned, 1))).toEqual(['2025-03-27'])
  })

  it('hands back nothing for any other underlying', () => {
    expect(selectionFor(owned, 2)).toEqual({})
    expect(selectionFor(owned, null)).toEqual({})
    expect(selectionFor({ underlyingId: null, selection: {} }, 1)).toEqual({})
  })
})
