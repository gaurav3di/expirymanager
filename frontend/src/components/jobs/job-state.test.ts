// @vitest-environment jsdom

// What the job screens claim about a job, checked against what the backend actually says.
//
// These screens turn eleven job statuses and seven task states into words and into buttons. The
// failure mode is not a crash: it is a screen that calls a parked job "failed" and sends the user
// looking for a bug that does not exist, or one that offers Retry on work that is about to resume
// by itself. So the vocabularies are not pinned as literals copied by hand. They are read out of
// the backend source at test time (the migration's CHECK constraints, the schema module's state
// table, the job service's action sets, the broker error classifier) and the frontend tables are
// asserted to reproduce them. A status added on the backend fails here instead of silently
// rendering as blocked.
//
// Nothing here is a credential and no fixture contains one.

import fs from 'node:fs'
import path from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import {
  JOB_KINDS,
  JOB_STATUSES,
  JobProgressHeader,
  anyJobLive,
  deriveJobState,
  jobCapabilities,
  jobProgress,
  pollIntervalFor,
} from '@/components/jobs/JobProgressHeader'
import type { JobRow } from '@/components/jobs/JobProgressHeader'
import { classifyTaskError } from '@/components/jobs/FailedTaskDrawer'
import {
  TASK_STATES,
  TASK_TABS,
  TaskTable,
  describeTaskState,
  tabByKey,
  tabCount,
} from '@/components/jobs/TaskTable'
import type { JobTaskRow } from '@/components/jobs/TaskTable'
import { queryKeys } from '@/lib/api/keys'
import type { EventFrame } from '@/lib/api/types'
import { applyEventFrame } from '@/lib/events/useEventStream'

// ---------------------------------------------------------------------------
// Reading the backend
// ---------------------------------------------------------------------------

const REPO_ROOT = path.resolve(import.meta.dirname, '../../../..')
const BACKEND = path.join(REPO_ROOT, 'backend', 'expirymanager')

function backendSource(...parts: string[]): string {
  const file = path.join(BACKEND, ...parts)
  // Read rather than required-if-present. A missing file means this test silently stopped
  // checking anything, which is the exact failure it exists to prevent.
  return fs.readFileSync(file, 'utf8')
}

/** Every `NAME = "value"` string constant in a Python module. */
function pythonStringConstants(source: string): Record<string, string> {
  const found: Record<string, string> = {}
  const pattern = /^([A-Z][A-Z0-9_]*) = "([^"]+)"$/gm
  let match = pattern.exec(source)
  while (match) {
    found[match[1]] = match[2]
    match = pattern.exec(source)
  }
  return found
}

/** The body of a top level `NAME: ... = { ... }` or `NAME: ... = ( ... )` assignment. */
function pythonBlock(source: string, name: string, open: '{' | '('): string {
  const marker = source.indexOf('\n' + name)
  if (marker < 0) {
    throw new Error('no assignment named ' + name + ' in the backend source')
  }
  const start = source.indexOf(open, marker)
  const close = open === '{' ? '}' : ')'
  const end = source.indexOf(close, start)
  return source.slice(start + 1, end)
}

/** `"key": CONSTANT,` entries, with the constant resolved to its string value. */
function pythonStringMap(block: string, constants: Record<string, string>): Record<string, string> {
  const found: Record<string, string> = {}
  const pattern = /"([a-z_]+)":\s*([A-Z][A-Z0-9_]*)/g
  let match = pattern.exec(block)
  while (match) {
    const resolved = constants[match[2]]
    if (resolved === undefined) {
      throw new Error('the constant ' + match[2] + ' has no string value in the backend source')
    }
    found[match[1]] = resolved
    match = pattern.exec(block)
  }
  return found
}

function quotedStrings(block: string): string[] {
  return [...block.matchAll(/["']([a-z_]+)["']/g)].map((match) => match[1])
}

/** `code: RetryClass.NAME,` entries out of one of the classifier maps. */
function retryClasses(source: string, name: string): Record<number, string> {
  const block = pythonBlock(source, name, '{')
  const found: Record<number, string> = {}
  const pattern = /^\s*(-?\d+):\s*RetryClass\.([A-Z_]+),/gm
  let match = pattern.exec(block)
  while (match) {
    found[Number(match[1])] = match[2]
    match = pattern.exec(block)
  }
  return found
}

/** The literals a `CHECK (column IN (...))` constraint admits, for one table. */
function checkConstraint(sql: string, table: string, column: string): string[] {
  const tableStart = sql.indexOf('CREATE TABLE ' + table + ' (')
  if (tableStart < 0) {
    throw new Error('no CREATE TABLE ' + table + ' in the migration')
  }
  const nextTable = sql.indexOf('CREATE TABLE ', tableStart + 1)
  const tableSql = sql.slice(tableStart, nextTable < 0 ? undefined : nextTable)
  const checkStart = tableSql.indexOf('CHECK (' + column + ' IN (')
  if (checkStart < 0) {
    throw new Error('no CHECK on ' + table + '.' + column)
  }
  const checkEnd = tableSql.indexOf('))', checkStart)
  return quotedStrings(tableSql.slice(checkStart, checkEnd))
}

const SCHEMA_SOURCE = backendSource('api', 'schemas', 'jobs.py')
const SERVICE_SOURCE = backendSource('pipeline', 'jobs.py')
const ERRORS_SOURCE = backendSource('brokers', 'fyers', 'errors.py')
const MIGRATION_SOURCE = backendSource('db', 'migrations', '0002_pipeline.sql')

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function jobRow(overrides: Partial<JobRow> = {}): JobRow {
  return {
    job_id: 'job-1',
    kind: 'candle_backfill',
    status: 'running',
    created_at: '2026-09-15T09:00:00',
    started_at: '2026-09-15T09:00:05',
    finished_at: null,
    total_tasks: 100,
    done_tasks: 40,
    empty_tasks: 5,
    failed_tasks: 0,
    skipped_tasks: 5,
    cancelled_tasks: 0,
    pending_tasks: 50,
    leased_tasks: 0,
    open_tasks: 50,
    est_requests: 100,
    requests_used: 50,
    rows_written: 52_873,
    bytes_downloaded: 1_048_576,
    throughput_per_minute: 96.4,
    eta_seconds: 300,
    reason: null,
    parent_job_id: null,
    schedule_id: null,
    priority: 100,
    created_by: 'user',
    params: null,
    ...overrides,
  }
}

function taskRow(overrides: Partial<JobTaskRow> = {}): JobTaskRow {
  return {
    task_id: 1,
    job_id: 'job-1',
    seq: 1,
    kind: 'candle_chunk',
    state: 'done',
    priority: 100,
    underlying_id: 1,
    contract_id: 42101,
    fyers_symbol: 'NSE:NIFTY25MAR23000CE',
    expiry_date: '2025-03-27',
    resolution: '1',
    range_from: '2025-03-01',
    range_to: '2025-03-27',
    include_oi: true,
    attempt: 1,
    max_attempts: 5,
    not_before: null,
    parent_task_id: null,
    http_status: null,
    error_code: null,
    error_message: null,
    latency_ms: 412,
    response_bytes: 90_112,
    row_count: 2_145,
    first_ts: '2025-03-03 09:15:00',
    last_ts: '2025-03-27 15:29:00',
    has_raw_body: false,
    request_params_json: null,
    started_at: '2026-09-15T09:01:00',
    finished_at: '2026-09-15T09:01:01',
    created_at: '2026-09-15T09:00:00',
    updated_at: '2026-09-15T09:01:01',
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// The vocabularies match the backend
// ---------------------------------------------------------------------------

describe('the job vocabulary this screen renders', () => {
  it('lists exactly the statuses the job table admits', () => {
    const fromSql = checkConstraint(MIGRATION_SOURCE, 'job', 'status')
    expect([...JOB_STATUSES].sort()).toEqual([...fromSql].sort())
  })

  it('lists exactly the kinds the job table admits', () => {
    const fromSql = checkConstraint(MIGRATION_SOURCE, 'job', 'kind')
    expect([...JOB_KINDS].sort()).toEqual([...fromSql].sort())
  })

  it('lists exactly the task states the task table admits', () => {
    const fromSql = checkConstraint(MIGRATION_SOURCE, 'task', 'state')
    expect([...TASK_STATES].sort()).toEqual([...fromSql].sort())
  })

  it('puts every status in the group the backend puts it in', () => {
    const constants = pythonStringConstants(SCHEMA_SOURCE)
    const groups = pythonStringMap(pythonBlock(SCHEMA_SOURCE, 'STATE_GROUPS', '{'), constants)
    expect(Object.keys(groups)).toHaveLength(JOB_STATUSES.length)
    for (const [status, group] of Object.entries(groups)) {
      expect(deriveJobState({ status }).group).toBe(group)
    }
  })

  it('gives every held status the block reason the backend gives it', () => {
    const constants = pythonStringConstants(SCHEMA_SOURCE)
    const blocks = pythonStringMap(pythonBlock(SCHEMA_SOURCE, '_BLOCK_CODES', '{'), constants)
    expect(Object.keys(blocks).sort()).toEqual(
      ['blocked_auth', 'blocked_rate', 'deferred_budget'].sort(),
    )
    for (const status of JOB_STATUSES) {
      expect(deriveJobState({ status }).blockReason).toBe(blocks[status] ?? null)
    }
  })

  it('offers exactly the actions the job service will accept', () => {
    const pausable = quotedStrings(pythonBlock(SERVICE_SOURCE, 'ACTIVE_JOB_STATUSES', '('))
    const resumable = quotedStrings(pythonBlock(SERVICE_SOURCE, 'RESUMABLE_JOB_STATUSES', '('))
    const terminal = quotedStrings(pythonBlock(SERVICE_SOURCE, 'TERMINAL_JOB_STATUSES', '('))
    expect(pausable.length).toBeGreaterThan(0)
    expect(resumable.length).toBeGreaterThan(0)
    expect(terminal.length).toBeGreaterThan(0)

    for (const status of JOB_STATUSES) {
      const can = jobCapabilities({ status, failed_tasks: 0 })
      expect({ status, ...can }).toEqual({
        status,
        canPause: pausable.includes(status),
        canResume: resumable.includes(status),
        canCancel: !terminal.includes(status),
        canRetryFailed: false,
      })
    }
  })
})

// ---------------------------------------------------------------------------
// The distinction the screen exists to make
// ---------------------------------------------------------------------------

describe('a job parked by the 03:00 IST logout', () => {
  const parked = jobRow({ status: 'blocked_auth', pending_tasks: 55, done_tasks: 40 })

  it('is not called failed and is not offered a retry', () => {
    const state = deriveJobState(parked)
    expect(state.label).toBe('Awaiting login')
    expect(state.isFailed).toBe(false)
    expect(state.group).toBe('blocked')
    expect(state.blockReason).toBe('authentication')
    expect(state.needsReauth).toBe(true)
    expect(state.meaning).toContain('log in')
    expect(state.meaning).not.toContain('failed')
    expect(jobCapabilities(parked).canRetryFailed).toBe(false)
  })

  it('offers resume, which is the action that actually moves it', () => {
    const can = jobCapabilities(parked)
    expect(can.canResume).toBe(true)
    expect(can.canPause).toBe(false)
    expect(can.canCancel).toBe(true)
  })

  it('reads differently from a rate limited stop and from a real failure', () => {
    const rateLimited = deriveJobState({ status: 'blocked_rate' })
    const failed = deriveJobState({ status: 'failed' })

    expect(rateLimited.blockReason).toBe('rate_limit')
    expect(rateLimited.needsReauth).toBe(false)
    expect(rateLimited.isFailed).toBe(false)
    expect(rateLimited.meaning).toContain('budget')

    expect(failed.isFailed).toBe(true)
    expect(failed.tone).toBe('bad')
    expect(failed.blockReason).toBeNull()

    const labels = new Set([
      deriveJobState(parked).label,
      rateLimited.label,
      failed.label,
      deriveJobState({ status: 'deferred_budget' }).label,
      deriveJobState({ status: 'paused' }).label,
    ])
    expect(labels.size).toBe(5)
  })

  it('reports an unrecognised status as unrecognised rather than as active', () => {
    const state = deriveJobState({ status: 'teleported' })
    expect(state.isKnown).toBe(false)
    expect(state.group).toBe('blocked')
    expect(state.meaning).toContain('teleported')
  })
})

// ---------------------------------------------------------------------------
// The stream patches the cache and the screen still reads the truth
// ---------------------------------------------------------------------------

describe('a stream frame landing in the cache these screens read', () => {
  function client(): QueryClient {
    return new QueryClient({ defaultOptions: { queries: { retry: false } } })
  }

  const listParams = { limit: 25 }

  it('patches the very row the list renders, by the key the list uses', () => {
    const cache = client()
    cache.setQueryData(queryKeys.jobs.list(listParams), {
      items: [jobRow()],
      next_cursor: null,
    })

    const frame = {
      event: 'job_progress',
      data: {
        job_id: 'job-1',
        status: 'running',
        total: 100,
        done: 61,
        empty: 5,
        failed: 2,
        skipped: 5,
        requests_used: 73,
        rows_written: 61_000,
        eta_seconds: 120,
      },
    } as unknown as EventFrame
    applyEventFrame(cache, frame)

    const patched = cache.getQueryData(queryKeys.jobs.list(listParams)) as { items: JobRow[] }
    const progress = jobProgress(patched.items[0])
    expect(patched.items[0].done_tasks).toBe(61)
    expect(progress.settled).toBe(73)
    expect(progress.percentLabel).toBe('73%')
    expect(jobCapabilities(patched.items[0]).canRetryFailed).toBe(true)
  })

  it('tells the truth about a job the stream has just parked, although the server fields are stale', () => {
    // This is the regression the derivation exists for. The REST body carried state_group
    // 'active', needs_reauth false and can_pause true; the frame changes only status and reason,
    // because that is all useEventStream writes. A screen reading the stale fields would keep
    // saying Running and keep offering Pause over a job nothing is working on.
    const cache = client()
    cache.setQueryData(
      queryKeys.jobs.detail('job-1'),
      jobRow({ state_group: 'active', needs_reauth: false, can_pause: true, can_resume: false }),
    )

    applyEventFrame(cache, {
      event: 'job_blocked',
      data: { job_id: 'job-1', status: 'blocked_auth', reason: 'broker token expired' },
    } as unknown as EventFrame)

    const patched = cache.getQueryData(queryKeys.jobs.detail('job-1')) as JobRow
    const state = deriveJobState(patched)
    expect(patched.state_group).toBe('active')
    expect(state.group).toBe('blocked')
    expect(state.needsReauth).toBe(true)
    expect(state.label).toBe('Awaiting login')
    expect(jobCapabilities(patched).canPause).toBe(false)
    expect(jobCapabilities(patched).canResume).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// REST stays authoritative
// ---------------------------------------------------------------------------

describe('the refetch cadence', () => {
  it('keeps polling every job that can still move', () => {
    expect(pollIntervalFor({ status: 'running' })).toBe(5_000)
    expect(pollIntervalFor({ status: 'queued' })).toBe(5_000)
    expect(pollIntervalFor({ status: 'paused' })).toBe(15_000)
    expect(pollIntervalFor({ status: 'blocked_auth' })).toBe(15_000)
    expect(pollIntervalFor({ status: 'blocked_rate' })).toBe(15_000)
    expect(pollIntervalFor({ status: 'deferred_budget' })).toBe(15_000)
  })

  it('stops polling a job that cannot change', () => {
    expect(pollIntervalFor({ status: 'completed' })).toBe(false)
    expect(pollIntervalFor({ status: 'completed_with_errors' })).toBe(false)
    expect(pollIntervalFor({ status: 'cancelled' })).toBe(false)
    expect(pollIntervalFor({ status: 'failed' })).toBe(false)
    expect(pollIntervalFor({ status: 'draft' })).toBe(false)
  })

  it('polls an unrecognised status rather than assuming it is finished', () => {
    expect(pollIntervalFor({ status: 'teleported' })).toBe(15_000)
  })

  it('polls a list holding one live job and leaves a settled list alone', () => {
    expect(anyJobLive([jobRow({ status: 'completed' }), jobRow({ status: 'failed' })])).toBe(false)
    expect(anyJobLive([jobRow({ status: 'completed' }), jobRow({ status: 'blocked_auth' })])).toBe(
      true,
    )
    expect(anyJobLive([])).toBe(false)
  })
})

describe('the progress arithmetic', () => {
  it('counts every terminal task as settled and never exceeds the bar', () => {
    const progress = jobProgress(
      jobRow({
        total_tasks: 58,
        done_tasks: 40,
        empty_tasks: 8,
        failed_tasks: 4,
        skipped_tasks: 6,
        cancelled_tasks: 0,
        pending_tasks: 0,
        leased_tasks: 0,
      }),
    )
    expect(progress.settled).toBe(58)
    expect(progress.total).toBe(58)
    expect(progress.fraction).toBe(1)
    expect(progress.percentLabel).toBe('100%')
    expect(progress.remaining).toBe(0)
  })

  it('never reads past the end of the bar when the counters run past the plan', () => {
    const progress = jobProgress(
      jobRow({
        total_tasks: 10,
        done_tasks: 12,
        empty_tasks: 0,
        failed_tasks: 0,
        skipped_tasks: 0,
        cancelled_tasks: 0,
        pending_tasks: 3,
        leased_tasks: 1,
      }),
    )
    expect(progress.total).toBe(12)
    expect(progress.settled).toBe(12)
    expect(progress.fraction).toBe(1)
    expect(progress.remaining).toBe(0)
  })

  it('does not move backwards when a stream frame patches only the settled counters', () => {
    // The frame carries done, empty, failed and skipped. It carries no pending and no leased, so
    // those sit at the value the last REST read left behind. A denominator built from them would
    // grow by exactly the tasks that just finished and the bar would crawl backwards.
    const before = jobProgress(
      jobRow({ total_tasks: 100, done_tasks: 40, empty_tasks: 0, skipped_tasks: 0, pending_tasks: 60 }),
    )
    const afterFrame = jobProgress(
      jobRow({ total_tasks: 100, done_tasks: 70, empty_tasks: 0, skipped_tasks: 0, pending_tasks: 60 }),
    )
    expect(before.percentLabel).toBe('40%')
    expect(afterFrame.percentLabel).toBe('70%')
    expect(afterFrame.fraction).toBeGreaterThan(before.fraction)
  })

  it('says nothing rather than zero when no estimate exists yet', () => {
    const progress = jobProgress(jobRow({ eta_seconds: null, throughput_per_minute: null }))
    expect(progress.etaLabel).toBe('not set')
    expect(progress.throughputLabel).toBe('not set')
  })
})

// ---------------------------------------------------------------------------
// Empty is not failed
// ---------------------------------------------------------------------------

describe('the task tabs', () => {
  it('renders empty distinctly from failed and does not ask for attention', () => {
    const empty = describeTaskState('empty')
    const failed = describeTaskState('failed')
    expect(empty.label).toBe('Empty')
    expect(failed.label).toBe('Failed')
    expect(empty.tone).not.toBe(failed.tone)
    expect(empty.needsAttention).toBe(false)
    expect(failed.needsAttention).toBe(true)
    expect(empty.meaning).toContain('no_data')
    expect(empty.meaning).toContain('not a failure')
  })

  it('names every task state the table can hold', () => {
    for (const state of TASK_STATES) {
      const reading = describeTaskState(state)
      expect(reading.label).not.toBe('')
      expect(reading.meaning.length).toBeGreaterThan(20)
    }
    expect(describeTaskState('teleported').meaning).toContain('teleported')
  })

  it('offers All, Failed, Empty and Skipped, each filtering on its own state', () => {
    expect(TASK_TABS.map((tab) => tab.label)).toEqual(['All', 'Failed', 'Empty', 'Skipped'])
    expect(TASK_TABS.map((tab) => tab.state)).toEqual([null, 'failed', 'empty', 'skipped'])
    expect(tabByKey('failed').state).toBe('failed')
    expect(tabByKey('nonsense').key).toBe('all')
  })

  it('counts from the live aggregate, not from the page on screen', () => {
    const states = { done: 900, empty: 61, failed: 3, skipped: 12, pending: 24 }
    expect(tabCount(states, tabByKey('all'))).toBe(1000)
    expect(tabCount(states, tabByKey('failed'))).toBe(3)
    expect(tabCount(states, tabByKey('empty'))).toBe(61)
    expect(tabCount(states, tabByKey('skipped'))).toBe(12)
    expect(tabCount({}, tabByKey('failed'))).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Why a task failed
// ---------------------------------------------------------------------------

describe('the failed task drill through', () => {
  const KIND_FOR_RETRY_CLASS: Record<string, string> = {
    FATAL: 'fatal',
    TRANSIENT: 'transient',
    AUTH_RECOVERABLE: 'auth',
    AUTH_FATAL: 'auth',
    RATE_LIMITED: 'rate_limit',
  }

  it('classifies every vendor code the pipeline classifies, the same way', () => {
    const codes = retryClasses(ERRORS_SOURCE, 'CODE_CLASSES')
    expect(Object.keys(codes).length).toBeGreaterThan(5)
    for (const [code, retryClass] of Object.entries(codes)) {
      const reading = classifyTaskError({
        state: 'failed',
        error_code: code,
        error_message: null,
        http_status: null,
      })
      expect({ code, kind: reading.kind }).toEqual({
        code,
        kind: KIND_FOR_RETRY_CLASS[retryClass],
      })
      // A screen that invited a retry on a fatal refusal would be inviting the user to spend a
      // request to be refused again.
      expect(reading.retryWorthwhile).toBe(retryClass !== 'FATAL')
    }
  })

  it('classifies every HTTP status the pipeline classifies, the same way', () => {
    const statuses = retryClasses(ERRORS_SOURCE, 'HTTP_CLASSES')
    expect(Object.keys(statuses).length).toBeGreaterThan(5)
    for (const [status, retryClass] of Object.entries(statuses)) {
      const reading = classifyTaskError({
        state: 'failed',
        error_code: null,
        error_message: null,
        http_status: Number(status),
      })
      expect({ status, kind: reading.kind }).toEqual({
        status,
        kind: KIND_FOR_RETRY_CLASS[retryClass],
      })
    }
  })

  it('prefers the vendor code over the HTTP status, because a refusal arrives inside a 200', () => {
    const reading = classifyTaskError({
      state: 'failed',
      error_code: '-300',
      error_message: 'invalid symbol',
      http_status: 200,
    })
    expect(reading.kind).toBe('fatal')
    expect(reading.explanation).toContain('does not know this symbol')
  })

  it('says it does not know rather than inventing a cause', () => {
    const reading = classifyTaskError({
      state: 'failed',
      error_code: '-9999',
      error_message: 'something new',
      http_status: null,
    })
    expect(reading.kind).toBe('unknown')
    expect(reading.retryWorthwhile).toBe(true)
  })

  it('explains an empty task as the success it is rather than as an error', () => {
    const reading = classifyTaskError(taskRow({ state: 'empty' }))
    expect(reading.kind).toBe('none')
    expect(reading.explanation).toContain('no_data')
    expect(reading.retryWorthwhile).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// What actually lands on the screen
// ---------------------------------------------------------------------------

describe('the rendered header', () => {
  function render(job: JobRow, brokerConnected: boolean): string {
    return renderToStaticMarkup(
      createElement(
        MemoryRouter,
        null,
        createElement(JobProgressHeader, { job, brokerConnected }),
      ),
    )
  }

  /** The text inside the first badge, which is the state badge. */
  function stateBadge(markup: string): string {
    const match = /data-slot="badge"[^>]*>([^<]*)</.exec(markup)
    return match ? match[1] : ''
  }

  it('says awaiting login and never says failed over a parked job', () => {
    const markup = render(
      jobRow({ status: 'blocked_auth', done_tasks: 40, pending_tasks: 60, eta_seconds: null }),
      false,
    )
    expect(stateBadge(markup)).toBe('Awaiting login')
    // The sentence a failed job would carry. A counter labelled Failed still renders, which is
    // correct: zero failures is a fact worth showing.
    expect(markup).not.toContain('does need attention')
    expect(markup).toContain('Log in to Fyers')
    expect(markup).toContain('continues by itself once you log in')
  })

  it('drops the login remedy once the token is back', () => {
    const markup = render(jobRow({ status: 'blocked_auth' }), true)
    expect(markup).toContain('Awaiting login')
    expect(markup).toContain('Resume to put this job back in the queue')
    expect(markup).not.toContain('Log in to Fyers')
  })

  it('renders the settled count and the percentage a user reads off the bar', () => {
    const markup = render(
      jobRow({
        status: 'running',
        total_tasks: 58,
        done_tasks: 40,
        empty_tasks: 5,
        failed_tasks: 2,
        skipped_tasks: 3,
        cancelled_tasks: 0,
        pending_tasks: 8,
        leased_tasks: 0,
      }),
      true,
    )
    expect(markup).toContain('50 of 58 tasks settled')
    expect(markup).toContain('86%')
    expect(markup).toContain('Remaining 8')
  })

  it('says the rate limited stop was deliberate', () => {
    const markup = render(jobRow({ status: 'blocked_rate' }), true)
    expect(markup).toContain('Rate limited')
    expect(markup).toContain('on purpose to protect the request budget')
  })
})

describe('the rendered task table', () => {
  function render(tasks: JobTaskRow[]): string {
    return renderToStaticMarkup(createElement(TaskTable, { tasks }))
  }

  it('gives an empty task a different badge from a failed one, in the same table', () => {
    const markup = render([
      taskRow({ task_id: 1, seq: 1, state: 'empty' }),
      taskRow({
        task_id: 2,
        seq: 2,
        state: 'failed',
        error_code: '-300',
        error_message: 'invalid symbol',
        http_status: 200,
        row_count: 0,
      }),
    ])
    const variants = [...markup.matchAll(/data-slot="badge" data-variant="([a-z]+)"/g)].map(
      (match) => match[1],
    )
    expect(variants).toEqual(['ghost', 'destructive'])
    expect(markup).toContain('>Empty<')
    expect(markup).toContain('>Failed<')
  })

  it('shows the per task error code and message on the row that failed', () => {
    const markup = render([
      taskRow({
        task_id: 2,
        state: 'failed',
        error_code: '-300',
        error_message: 'invalid symbol',
        http_status: 200,
        attempt: 5,
        max_attempts: 5,
      }),
    ])
    expect(markup).toContain('-300 HTTP 200')
    expect(markup).toContain('invalid symbol')
    expect(markup).toContain('5 of 5')
    expect(markup).toContain('NSE:NIFTY25MAR23000CE')
  })

  it('never renders a raw body path, because the route never sends one', () => {
    const markup = render([taskRow({ has_raw_body: true })])
    expect(markup).not.toContain('raw_body_path')
    expect(markup).not.toContain('.json')
  })
})
