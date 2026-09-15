// @vitest-environment jsdom

// Two things on the schedules screen are worth pinning, and both of them are about honesty
// rather than about layout.
//
// The cron preview is the only place the user is told WHEN something runs. A wrong sentence
// there is worse than the expression itself, so every expression this app actually ships is
// asserted by its exact rendered string, and an expression the describer does not understand is
// asserted to fall back to the raw expression rather than to a guess.
//
// The run description is the only place a maintenance run that reported `completed` while its
// own note says the trading day refresh FAILED can surface. The note format asserted here is the
// one scheduler/jobs_def.run_maintenance actually writes.

import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  concerningNoteParts,
  describeCron,
  describeRun,
  isScheduledLogout,
} from '@/routes/schedules'
import type { ScheduleRow } from '@/routes/schedules'
import { buildScheduleBody, cronPreview } from '@/components/schedules/ScheduleDialog'
import type { FormState } from '@/components/schedules/ScheduleDialog'

const IST = 'Asia/Kolkata'

describe('describeCron', () => {
  it('reads every cron the seeded schedules ship with', () => {
    // These are the expressions in db/migrations/0005_schedules.sql, verbatim.
    const cases: Array<[string, string]> = [
      ['0 3 * * *', 'Every day at 03:00 IST'],
      ['1 0 * * *', 'Every day at 00:01 IST'],
      ['0 2 * * *', 'Every day at 02:00 IST'],
      ['15 16 * * 1-5', 'Monday to Friday at 16:15 IST'],
      ['0 18 * * 1-5', 'Monday to Friday at 18:00 IST'],
      ['15 18 * * 1-5', 'Monday to Friday at 18:15 IST'],
      ['30 18 * * 1-5', 'Monday to Friday at 18:30 IST'],
      ['45 18 * * 1-5', 'Monday to Friday at 18:45 IST'],
      ['25 15 * * 1-5', 'Monday to Friday at 15:25 IST'],
      ['0 * * * *', 'Every hour, on the hour IST'],
      ['0 7 * * 0', 'Every Sunday at 07:00 IST'],
    ]
    for (const [expression, expected] of cases) {
      expect(describeCron(expression, IST)).toBe(expected)
    }
  })

  it('names a timezone that is not IST instead of implying one', () => {
    expect(describeCron('0 3 * * *', 'UTC')).toBe('Every day at 03:00 UTC')
    expect(describeCron('0 3 * * *')).toBe('Every day at 03:00')
  })

  it('reads steps, lists and days of the month', () => {
    expect(describeCron('*/5 * * * *', IST)).toBe('Every 5 minutes IST')
    expect(describeCron('0 9,15 * * 1-5', IST)).toBe(
      'Monday to Friday at 09:00 and 15:00 IST',
    )
    expect(describeCron('30 6 1 * *', IST)).toBe('Day 1 of the month at 06:30 IST')
    expect(describeCron('0 8 * * 6,0', IST)).toBe('Sunday and Saturday at 08:00 IST')
  })

  it('returns the expression unchanged rather than guessing at a form it cannot read', () => {
    // A wrong sentence about when a job runs is worse than the expression the scheduler is
    // actually holding.
    for (const expression of ['0 3 * * 1#2', '0 3 * * MON', '0 3 1 6 *', 'not a cron', '0 3 * *']) {
      expect(describeCron(expression, IST)).toBe(expression)
    }
  })

  it('says so in the dialog preview when it cannot read the expression', () => {
    expect(cronPreview('0 3 * * *', IST)).toBe('Every day at 03:00 IST')
    expect(cronPreview('0 3 * * MON', IST)).toContain('cannot put that expression into words')
  })
})

describe('concerningNoteParts', () => {
  // Copied verbatim from a real run-now against a live instance of this backend, so the format
  // asserted here is the format the product actually writes and not a guess at it.
  const cleanNote =
    'wal 27522 to 0 bytes; 0 trading days derived from spot bars; ' +
    '0 health checks with rows; 0 duplicate groups; 0 coverage mismatches; 0 rate events pruned'

  const badNote =
    'wal 1048576 to 0 bytes; trading day refresh FAILED: OperationalError; ' +
    '2 health checks with rows; 0 duplicate groups; 3 coverage mismatches; 12 rate events pruned'

  it('finds nothing in a clean maintenance note', () => {
    expect(concerningNoteParts(cleanNote)).toEqual([])
    expect(concerningNoteParts(null)).toEqual([])
    expect(concerningNoteParts('token cleared, running jobs parked')).toEqual([])
  })

  it('picks out the failure and the non zero findings, verbatim', () => {
    expect(concerningNoteParts(badNote)).toEqual([
      'trading day refresh FAILED: OperationalError',
      '2 health checks with rows',
      '3 coverage mismatches',
    ])
  })
})

describe('describeRun', () => {
  it('refuses to draw a completed maintenance run with a failed note as plain success', () => {
    const described = describeRun(
      'completed',
      'wal 10 to 0 bytes; trading day refresh FAILED: OperationalError; 0 duplicate groups',
      'maintenance',
    )
    expect(described.tone).toBe('attention')
    expect(described.label).toBe('Completed with findings')
    expect(described.findings).toEqual(['trading day refresh FAILED: OperationalError'])
  })

  it('draws a genuinely clean run as a clean run', () => {
    const described = describeRun(
      'completed',
      '0 health checks with rows; 0 duplicate groups; 0 coverage mismatches',
      'maintenance',
    )
    expect(described.tone).toBe('ok')
    expect(described.label).toBe('Completed')
    expect(described.findings).toEqual([])
  })

  it('treats the nightly logout as the expected event it is', () => {
    const described = describeRun('completed', 'token cleared, running jobs parked', 'token_logout')
    expect(described.tone).toBe('ok')
    expect(described.meaning).toContain('parked')
    expect(described.meaning).toContain('resumes after the next login')
  })

  it('explains a fire skipped for want of a token instead of calling it an error', () => {
    // The note is the one a live rolling_backfill fire wrote with no token stored.
    const described = describeRun(
      'skipped_needs_auth',
      'the Fyers access token is missing or expired, so the run was skipped rather than spent ' +
        'on requests that would be rejected',
      'rolling_backfill',
    )
    expect(described.tone).toBe('skipped')
    expect(described.meaning).toContain('daily logout')
    expect(described.meaning).toContain('Nothing was lost')
  })

  it('still reports a real error as an error', () => {
    const described = describeRun('error', 'planner raised', 'rolling_backfill')
    expect(described.tone).toBe('error')
    expect(described.label).toBe('Error')
  })

  it('reports an enqueued fire as work rather than as a finished run', () => {
    expect(describeRun('enqueued', '3 jobs created', 'expiry_discovery').tone).toBe('pending')
  })
})

describe('the screen uses what these functions produce', () => {
  // The failure this codebase keeps hitting is a correct function nobody calls. These read the
  // shipped source so a refactor that stops rendering the findings, or stops reading the run
  // that carries them, fails here rather than going quiet on screen.
  const routeSource = fs.readFileSync(
    path.join(import.meta.dirname, 'schedules.tsx'),
    'utf8',
  )
  const drawerSource = fs.readFileSync(
    path.join(import.meta.dirname, '..', 'components', 'schedules', 'RunHistoryDrawer.tsx'),
    'utf8',
  )

  it('reads the newest run of every schedule, because the schedule row carries no note', () => {
    expect(routeSource).toContain("'/schedules/' + scheduleId + '/runs'")
    expect(routeSource).toContain('query: { limit: 1 }')
  })

  it('renders the findings in the table and in the drawer', () => {
    expect(routeSource).toContain('lastRun.findings.map')
    expect(drawerSource).toContain('described.findings.map')
  })

  it('renders the cron preview rather than only the expression', () => {
    expect(routeSource).toContain('describeCron(row.cron, row.timezone)')
  })
})

describe('isScheduledLogout', () => {
  it('matches the kind and not the name, which a user can rename', () => {
    const row = { kind: 'token_logout', name: 'Something else' } as ScheduleRow
    expect(isScheduledLogout(row)).toBe(true)
    expect(isScheduledLogout({ kind: 'maintenance' } as ScheduleRow)).toBe(false)
  })
})

describe('buildScheduleBody', () => {
  const form: FormState = {
    name: 'Nightly backfill',
    kind: 'rolling_backfill',
    cron: '30  18 * * 1-5',
    timezone: IST,
    enabled: true,
    trading_days_only: true,
    misfire_grace_seconds: '3600',
    max_requests_per_run: '',
    params: '{"max_expiries": 40}',
  }

  it('sends kind on a create and never on an edit', () => {
    const created = buildScheduleBody(form, 'create')
    expect('body' in created && created.body.kind).toBe('rolling_backfill')
    const edited = buildScheduleBody(form, 'edit')
    // The backend's update model has no kind field, so sending one fails an otherwise good edit.
    expect('body' in edited && 'kind' in edited.body).toBe(false)
  })

  it('normalises the whitespace in the expression it sends', () => {
    const built = buildScheduleBody(form, 'create')
    expect('body' in built && built.body.cron).toBe('30 18 * * 1-5')
  })

  it('sends an empty request ceiling as null and not as zero', () => {
    // 0 would be a schedule allowed no requests at all, which is a schedule that can never work.
    const built = buildScheduleBody(form, 'create')
    expect('body' in built && built.body.max_requests_per_run).toBeNull()
    const capped = buildScheduleBody({ ...form, max_requests_per_run: '5000' }, 'create')
    expect('body' in capped && capped.body.max_requests_per_run).toBe(5000)
  })

  it('refuses the shapes the backend would refuse, before spending a request on them', () => {
    expect(buildScheduleBody({ ...form, name: '   ' }, 'create')).toHaveProperty('error')
    expect(buildScheduleBody({ ...form, cron: '30 18 * *' }, 'create')).toHaveProperty('error')
    expect(buildScheduleBody({ ...form, params: '[1,2]' }, 'create')).toHaveProperty('error')
    expect(buildScheduleBody({ ...form, params: 'not json' }, 'create')).toHaveProperty('error')
    expect(
      buildScheduleBody({ ...form, misfire_grace_seconds: '999999' }, 'create'),
    ).toHaveProperty('error')
    expect(
      buildScheduleBody({ ...form, max_requests_per_run: '0' }, 'create'),
    ).toHaveProperty('error')
  })
})
