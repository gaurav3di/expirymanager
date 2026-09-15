// @vitest-environment jsdom

// The dashboard answers "what do I have and what is happening" without a click, so the things
// worth pinning are the judgements it makes on the way to that answer:
//
//  - a job parked by the nightly logout is offered a login, not a Resume button that would fail
//    the auth gate again, and it is never drawn as a failure;
//  - the coverage rollup sums rows across cells and contracts only within a resolution, because
//    a contract appears once per resolution and summing across them invents contracts;
//  - the next-fire list only contains schedules that are actually enabled and actually have a
//    next fire.

import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  bucketJobs,
  describeInterruption,
  nextFires,
  settledFraction,
  summariseGrid,
} from '@/routes/dashboard'
import type { JobSummary } from '@/routes/dashboard'
import type { CoverageGrid } from '@/components/exports/ExportDialog'
import type { ScheduleRow } from '@/routes/schedules'

function job(overrides: Partial<JobSummary>): JobSummary {
  return {
    job_id: 'job_1',
    kind: 'candle_download',
    status: 'running',
    created_at: '2025-03-27T09:00:00',
    started_at: '2025-03-27T09:00:05',
    finished_at: null,
    total_tasks: 100,
    done_tasks: 0,
    empty_tasks: 0,
    failed_tasks: 0,
    skipped_tasks: 0,
    requests_used: 0,
    rows_written: 0,
    throughput_per_minute: null,
    eta_seconds: null,
    reason: null,
    state_group: 'active',
    needs_reauth: false,
    blocked_reason: null,
    is_failed: false,
    has_failures: false,
    can_pause: true,
    can_resume: false,
    can_cancel: true,
    can_retry_failed: false,
    ...overrides,
  }
}

describe('bucketJobs', () => {
  it('separates what is running from what stopped without finishing', () => {
    const parked = job({
      job_id: 'job_parked',
      status: 'blocked_auth',
      state_group: 'blocked',
      needs_reauth: true,
      blocked_reason: 'authentication',
      can_resume: true,
    })
    const paused = job({ job_id: 'job_paused', status: 'paused', state_group: 'paused', can_resume: true })
    const done = job({
      job_id: 'job_done',
      status: 'completed',
      state_group: 'terminal',
      finished_at: '2025-03-27T09:40:00',
    })
    const buckets = bucketJobs([job({}), parked, paused, done])

    expect(buckets.running.map((item) => item.job_id)).toEqual(['job_1'])
    expect(buckets.interrupted.map((item) => item.job_id)).toEqual(['job_parked', 'job_paused'])
    expect(buckets.finished.map((item) => item.job_id)).toEqual(['job_done'])
  })

  it('treats a job waiting for the next budget day as interrupted, not as finished', () => {
    const deferred = job({
      status: 'deferred_budget',
      state_group: 'deferred',
      blocked_reason: 'budget',
    })
    expect(bucketJobs([deferred]).interrupted).toHaveLength(1)
    expect(bucketJobs([deferred]).finished).toHaveLength(0)
  })
})

describe('describeInterruption', () => {
  it('offers the login, not a resume, for a job the nightly logout parked', () => {
    // The 03:00 IST logout parks running jobs on purpose. Pressing Resume would meet the same
    // auth gate; storing a token is what actually restarts it.
    const described = describeInterruption(
      job({
        status: 'blocked_auth',
        state_group: 'blocked',
        needs_reauth: true,
        blocked_reason: 'authentication',
        can_resume: true,
      }),
    )
    expect(described.action).toBe('sign_in')
    expect(described.headline).toBe('Waiting for a Fyers login')
    expect(described.meaning).toContain('parked, not failed')
  })

  it('says a parked job lost nothing, in as many words', () => {
    const described = describeInterruption(job({ needs_reauth: true, state_group: 'blocked' }))
    expect(described.meaning).toContain('picks up at the next task')
  })

  it('offers Resume for a job paused by hand', () => {
    const described = describeInterruption(
      job({ status: 'paused', state_group: 'paused', can_resume: true }),
    )
    expect(described.action).toBe('resume')
    expect(described.actionLabel).toBe('Resume')
  })

  it('offers nothing to press while the budget is spent, and says when it restarts', () => {
    const described = describeInterruption(
      job({ status: 'deferred_budget', state_group: 'deferred', blocked_reason: 'budget' }),
    )
    expect(described.action).toBe('wait')
    expect(described.meaning).toContain('IST date rolls')
  })

  it('offers the retry only when there are failed tasks to retry', () => {
    const described = describeInterruption(
      job({
        status: 'failed',
        state_group: 'terminal',
        is_failed: true,
        has_failures: true,
        failed_tasks: 3,
        can_retry_failed: true,
      }),
    )
    expect(described.action).toBe('retry_failed')
    expect(described.headline).toBe('3 tasks failed')
  })
})

describe('settledFraction', () => {
  it('counts empty, failed and skipped tasks as settled', () => {
    // An expired contract that Fyers has nothing for answers empty, and that is a final answer.
    // Counting only done_tasks would leave a finished job showing a part filled bar forever.
    expect(
      settledFraction(
        job({ total_tasks: 10, done_tasks: 4, empty_tasks: 4, failed_tasks: 1, skipped_tasks: 1 }),
      ),
    ).toBe(1)
  })

  it('is zero rather than NaN before the task rows exist', () => {
    expect(settledFraction(job({ total_tasks: 0 }))).toBe(0)
  })
})

describe('summariseGrid', () => {
  const grid: CoverageGrid = {
    underlying_id: 1,
    resolutions: [
      { res_id: 2, fyers_code: '1', label: '1 minute', seconds: 60 },
      { res_id: 6, fyers_code: 'D', label: '1 day', seconds: 86400 },
    ],
    cells: [
      {
        expiry_date: '2025-03-27',
        res_id: 2,
        contracts_total: 40,
        contracts_with_data: 36,
        contracts_missing: 4,
        chunks_ok: 100,
        chunks_empty: 4,
        chunks_error: 1,
        rows: 52_873,
      },
      {
        expiry_date: '2025-04-24',
        res_id: 2,
        contracts_total: 30,
        contracts_with_data: 10,
        contracts_missing: 20,
        chunks_ok: 20,
        chunks_empty: 0,
        chunks_error: 0,
        rows: 8_000,
      },
      {
        expiry_date: '2025-03-27',
        res_id: 6,
        contracts_total: 40,
        contracts_with_data: 40,
        contracts_missing: 0,
        chunks_ok: 40,
        chunks_empty: 0,
        chunks_error: 0,
        rows: 400,
      },
    ],
  }

  it('sums every candle in the ledger exactly once', () => {
    expect(summariseGrid(grid).rows).toBe(61_273)
    expect(summariseGrid(grid).expiriesWithData).toBe(2)
  })

  it('sums contracts within a resolution and never across them', () => {
    // 36 + 10 for the minute series, 40 for the day series. A total of 86 would be the double
    // count: the same 40 contracts appear once per resolution.
    const summary = summariseGrid(grid)
    const minute = summary.resolutions.find((item) => item.res_id === 2)
    const daily = summary.resolutions.find((item) => item.res_id === 6)
    expect(minute?.contractsWithData).toBe(46)
    expect(minute?.contractsTotal).toBe(70)
    expect(daily?.contractsWithData).toBe(40)
    expect(daily?.contractsTotal).toBe(40)
  })

  it('keeps the errored windows visible instead of folding them into the ok count', () => {
    const minute = summariseGrid(grid).resolutions.find((item) => item.res_id === 2)
    expect(minute?.chunksOk).toBe(120)
    expect(minute?.chunksEmpty).toBe(4)
    expect(minute?.chunksError).toBe(1)
  })

  it('is empty rather than throwing before a grid has loaded', () => {
    expect(summariseGrid(undefined)).toEqual({ rows: 0, expiriesWithData: 0, resolutions: [] })
  })
})

describe('nextFires', () => {
  function schedule(overrides: Partial<ScheduleRow>): ScheduleRow {
    return {
      schedule_id: 'builtin_maintenance',
      name: 'Maintenance',
      kind: 'maintenance',
      cron: '0 2 * * *',
      timezone: 'Asia/Kolkata',
      enabled: true,
      trading_days_only: false,
      misfire_grace_seconds: 3600,
      max_requests_per_run: null,
      is_builtin: true,
      params: {},
      next_fire_at: '2025-03-28T02:00:00+05:30',
      last_fired_at: null,
      last_outcome: null,
      last_job_id: null,
      description: null,
      ...overrides,
    }
  }

  it('is soonest first', () => {
    const fires = nextFires(
      [
        schedule({ schedule_id: 'late', next_fire_at: '2025-03-28T18:30:00+05:30' }),
        schedule({ schedule_id: 'soon', next_fire_at: '2025-03-28T02:00:00+05:30' }),
      ],
      5,
    )
    expect(fires.map((fire) => fire.schedule.schedule_id)).toEqual(['soon', 'late'])
  })

  it('leaves out a schedule that is off, and one the scheduler gave no next fire', () => {
    const fires = nextFires(
      [
        schedule({ schedule_id: 'off', enabled: false }),
        schedule({ schedule_id: 'unscheduled', next_fire_at: null }),
        schedule({ schedule_id: 'on' }),
      ],
      5,
    )
    expect(fires.map((fire) => fire.schedule.schedule_id)).toEqual(['on'])
  })

  it('shows only as many as the screen asked for', () => {
    const many = Array.from({ length: 9 }, (_unused, index) =>
      schedule({
        schedule_id: 'id_' + String(index),
        next_fire_at: '2025-03-2' + String(index % 9) + 'T02:00:00+05:30',
      }),
    )
    expect(nextFires(many, 6)).toHaveLength(6)
  })
})

describe('the screen uses what these functions produce', () => {
  const source = fs.readFileSync(path.join(import.meta.dirname, 'dashboard.tsx'), 'utf8')

  it('renders the interruption decision rather than switching on the status string', () => {
    expect(source).toContain('const interruption = describeInterruption(job)')
    expect(source).toContain("interruption.action === 'sign_in'")
    expect(source).toContain("interruption.action === 'resume'")
  })

  it('reads coverage from the ledger for every active underlying', () => {
    expect(source).toContain("api.get<CoverageGrid>('/coverage/grid'")
    expect(source).toContain('summariseGrid(grids.get(row.underlying_id))')
  })

  it('shows what fires next', () => {
    expect(source).toContain('nextFires(schedules.data ?? [], NEXT_FIRE_COUNT)')
  })

  it('resumes and retries through the job routes rather than inventing a local state', () => {
    expect(source).toContain("'/jobs/' + jobId + '/resume'")
    expect(source).toContain("'/jobs/' + jobId + '/retry-failed'")
  })
})
