import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from 'cn'

import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import {
  EMPTY_VALUE,
  formatDateTime,
  formatDuration,
  formatInteger,
  formatPercent,
  humaniseCode,
  safeRatio,
} from '@/lib/format'

// How a job reads, and the header that reads it.
//
// This module is the single place the job vocabulary is turned into words and into buttons. Two
// screens render it and both get the same answer, which is the whole reason it is not inlined.
//
// WHY THE STATUS IS RE-READ HERE RATHER THAN TRUSTED FROM THE SERVER FIELDS.
// The REST body already carries state_group, needs_reauth, blocked_reason and the four can_*
// gates, computed by api/schemas/jobs.py. But the SSE stream does not: a job_blocked frame
// patches status and reason into the cached row and leaves every derived field at whatever the
// last REST read said. A screen that trusted state_group would therefore show "Active" over a
// job the stream has just told it is parked in blocked_auth, and would keep offering Pause on a
// job nothing is working on. So the reading is derived from `status`, which the stream does
// maintain, and job-state.test.ts reads the backend's own tables off disk and asserts this
// module reproduces them for every status. Drift fails a test rather than mislabels a screen.
//
// THE DISTINCTION THAT MATTERS MOST.
// `blocked_auth` is not failure. The 03:00 IST logout parks every running job there and they
// resume by themselves after the next login. Calling that "failed" sends the user hunting for a
// bug that does not exist. `blocked_rate` is different again: the pipeline stopped on purpose to
// protect the day's request budget, and nothing is wrong at all. `failed` is reserved for a job
// that genuinely cannot continue.

export type JobStateGroup = 'draft' | 'active' | 'paused' | 'blocked' | 'deferred' | 'terminal'

export type JobBlockReason = 'authentication' | 'rate_limit' | 'budget'

/** Badge tone. Plain names rather than colours, so a rename of the palette is not a rename here. */
export type StateTone = 'neutral' | 'running' | 'attention' | 'good' | 'bad'

/** PIPELINE.md section 9.1, in the order a job moves through them. */
export const JOB_STATUSES = [
  'draft',
  'queued',
  'running',
  'paused',
  'blocked_auth',
  'blocked_rate',
  'deferred_budget',
  'completed',
  'completed_with_errors',
  'cancelled',
  'failed',
] as const

export type JobStatusName = (typeof JOB_STATUSES)[number]

/**
 * Every kind the `job.kind` CHECK constraint admits, in the order it lists them.
 *
 * The filter on the job list is built from this rather than from the kinds present on the page
 * in front of the user, so filtering to a kind that has not run yet is possible and answers
 * "none yet" instead of being unofferable. job-state.test.ts reads the constraint out of the
 * migration and asserts this list is exactly it.
 */
export const JOB_KINDS = [
  'expiry_discovery',
  'contract_discovery',
  'candle_backfill',
  'underlying_history',
  'symbol_master',
  'seconds_capture',
  'chain_snapshot',
  'gap_repair',
  'export',
] as const

export type JobKindName = (typeof JOB_KINDS)[number]

/**
 * One row of `GET /api/v1/jobs`.
 *
 * Declared here rather than imported from lib/api/types.ts, which W21 wrote against the API.md
 * sketch and which predates the route: it omits state_group, needs_reauth, blocked_reason, the
 * can_* gates, cancel_requested, est_requests, bytes_downloaded and the open task counters, all
 * of which the served body carries and this screen reads. `status` is widened to string because
 * the wire can carry a status this build has never heard of, and pretending otherwise would put
 * a cast at every switch.
 */
export interface JobRow {
  job_id: string
  kind: string
  status: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null

  total_tasks: number
  done_tasks: number
  empty_tasks: number
  failed_tasks: number
  skipped_tasks: number
  cancelled_tasks: number
  pending_tasks: number
  leased_tasks: number
  open_tasks: number

  est_requests: number
  requests_used: number
  rows_written: number
  bytes_downloaded: number

  throughput_per_minute: number | null
  eta_seconds: number | null

  reason: string | null
  parent_job_id: string | null
  schedule_id: string | null
  priority: number
  created_by: string | null
  params: Record<string, unknown> | null

  // Present on the wire, deliberately not read. See the note at the top of this file.
  state_group?: string
  needs_reauth?: boolean
  blocked_reason?: string | null
  is_failed?: boolean
  has_failures?: boolean
  cancel_requested?: boolean
  can_pause?: boolean
  can_resume?: boolean
  can_cancel?: boolean
  can_retry_failed?: boolean
}

/** `GET /api/v1/jobs/{job_id}`. `task_states` is a fresh GROUP BY over the task table. */
export interface JobDetailRow extends JobRow {
  task_states: Record<string, number>
  child_job_ids: string[]
  error_text: string | null
}

export interface JobStateReading {
  status: string
  group: JobStateGroup
  label: string
  tone: StateTone
  /** One sentence: what is happening, and what happens next. Rendered verbatim. */
  meaning: string
  blockReason: JobBlockReason | null
  needsReauth: boolean
  isFailed: boolean
  /** True while the job can still change on its own, which is what drives the refetch cadence. */
  isLive: boolean
  /** True when the job is recognised. False means this build is behind the pipeline. */
  isKnown: boolean
}

type StaticReading = Pick<JobStateReading, 'group' | 'label' | 'tone' | 'meaning' | 'blockReason'>

const READINGS: Record<JobStatusName, StaticReading> = {
  draft: {
    group: 'draft',
    label: 'Draft',
    tone: 'neutral',
    meaning: 'Planned but never committed. No tasks are queued and no requests will be spent.',
    blockReason: null,
  },
  queued: {
    group: 'active',
    label: 'Queued',
    tone: 'running',
    meaning: 'Tasks are written and waiting for a worker to lease them.',
    blockReason: null,
  },
  running: {
    group: 'active',
    label: 'Running',
    tone: 'running',
    meaning: 'Workers are leasing tasks and writing candles.',
    blockReason: null,
  },
  paused: {
    group: 'paused',
    label: 'Paused',
    tone: 'attention',
    meaning: 'Leasing stopped. Not one task row was rewritten, so resume costs nothing.',
    blockReason: null,
  },
  blocked_auth: {
    group: 'blocked',
    label: 'Awaiting login',
    tone: 'attention',
    // The sentence this whole module exists for.
    meaning:
      'The broker token is gone, so the pipeline parked this job instead of failing it. ' +
      'Every task is still pending and the job continues by itself once you log in to Fyers again.',
    blockReason: 'authentication',
  },
  blocked_rate: {
    group: 'blocked',
    label: 'Rate limited',
    tone: 'attention',
    meaning:
      'The pipeline stopped on purpose to protect the request budget after the vendor refused ' +
      'too many requests. Nothing is broken and no work was lost. Resume when the window has passed.',
    blockReason: 'rate_limit',
  },
  deferred_budget: {
    group: 'deferred',
    label: 'Held for tomorrow',
    tone: 'attention',
    meaning:
      'Committed but held back because it does not fit in what is left of today. ' +
      'It moves to queued by itself at 00:01 IST.',
    blockReason: 'budget',
  },
  completed: {
    group: 'terminal',
    label: 'Completed',
    tone: 'good',
    meaning: 'Every task reached a terminal state and none failed.',
    blockReason: null,
  },
  completed_with_errors: {
    group: 'terminal',
    label: 'Completed with errors',
    tone: 'attention',
    meaning:
      'Every task finished, but some failed. The data that did arrive is written and the ' +
      'coverage ledger records exactly which windows are missing.',
    blockReason: null,
  },
  cancelled: {
    group: 'terminal',
    label: 'Cancelled',
    tone: 'neutral',
    meaning:
      'Stopped on request. Tasks that were in flight finished and wrote their data, so the ' +
      'partial result is valid and the ledger describes it accurately.',
    blockReason: null,
  },
  failed: {
    group: 'terminal',
    label: 'Failed',
    tone: 'bad',
    meaning: 'The job itself could not run. This one does need attention.',
    blockReason: null,
  },
}

const LIVE_GROUPS: readonly JobStateGroup[] = ['active', 'paused', 'blocked', 'deferred']

function isKnownStatus(status: string): status is JobStatusName {
  return Object.prototype.hasOwnProperty.call(READINGS, status)
}

/**
 * How one job reads.
 *
 * An unrecognised status is reported as blocked and named as unrecognised, never as active. The
 * same rule the backend uses, and for the same reason: a spinner over a job nothing is working
 * on is a lie, while "this build does not know this status" is the truth.
 */
export function deriveJobState(job: Pick<JobRow, 'status'>): JobStateReading {
  const status = job.status
  if (!isKnownStatus(status)) {
    return {
      status,
      group: 'blocked',
      label: humaniseCode(status),
      tone: 'attention',
      meaning:
        'This build does not recognise the status ' +
        status +
        '. Nothing is being assumed about it. The backend is ahead of this screen.',
      blockReason: null,
      needsReauth: false,
      isFailed: false,
      isLive: true,
      isKnown: false,
    }
  }
  const reading = READINGS[status]
  return {
    ...reading,
    status,
    needsReauth: status === 'blocked_auth',
    isFailed: status === 'failed',
    isLive: LIVE_GROUPS.includes(reading.group),
    isKnown: true,
  }
}

export interface JobCapabilities {
  canPause: boolean
  canResume: boolean
  canCancel: boolean
  canRetryFailed: boolean
}

const PAUSABLE: readonly string[] = ['queued', 'running']
const RESUMABLE: readonly string[] = ['paused', 'blocked_auth', 'blocked_rate', 'deferred_budget']
const TERMINAL: readonly string[] = ['completed', 'completed_with_errors', 'cancelled', 'failed']

/** Which of the four commands the job service will actually accept right now. */
export function jobCapabilities(job: Pick<JobRow, 'status' | 'failed_tasks'>): JobCapabilities {
  return {
    canPause: PAUSABLE.includes(job.status),
    canResume: RESUMABLE.includes(job.status),
    canCancel: !TERMINAL.includes(job.status),
    canRetryFailed: job.failed_tasks > 0,
  }
}

export interface JobProgress {
  /** Tasks that reached a terminal state, however they got there. */
  settled: number
  total: number
  /** 0 to 1, clamped. */
  fraction: number
  percentLabel: string
  remaining: number
  etaLabel: string
  throughputLabel: string
}

/**
 * The progress arithmetic, done once.
 *
 * The denominator is `total_tasks`, the planned count written into the job row in the same
 * transaction as the task rows, so it is exact and it never moves. Deriving it instead from
 * settled plus pending plus leased would make the bar jump backwards under the stream: a
 * job_progress frame carries the settled counters and not the open ones, so the open counters sit
 * at the value the last REST read left them at while settled climbs, and the denominator would
 * inflate by exactly the tasks that have just finished.
 *
 * The one guard: if the live settled count somehow runs past the planned total, the total grows
 * to meet it, because a bar reading 110 percent looks like corruption.
 */
export function jobProgress(job: JobRow): JobProgress {
  const settled =
    job.done_tasks + job.empty_tasks + job.failed_tasks + job.skipped_tasks + job.cancelled_tasks
  const total = Math.max(job.total_tasks, settled)
  const fraction = safeRatio(settled, total)
  return {
    settled,
    total,
    fraction,
    percentLabel: formatPercent(fraction),
    remaining: Math.max(0, total - settled),
    etaLabel: job.eta_seconds === null ? EMPTY_VALUE : formatDuration(job.eta_seconds),
    throughputLabel:
      job.throughput_per_minute === null
        ? EMPTY_VALUE
        : formatInteger(Math.round(job.throughput_per_minute)) + ' per minute',
  }
}

/**
 * How often the screen refetches this job over REST, in milliseconds, or false for never.
 *
 * REST is authoritative and the stream is only an accelerator, so every job that can still move
 * is polled whether or not the stream is delivering. A dropped stream degrades the refresh rate
 * and nothing else. A terminal job cannot change, so it is not polled at all.
 */
export function pollIntervalFor(job: Pick<JobRow, 'status'>): number | false {
  const state = deriveJobState(job)
  if (!state.isLive) {
    return false
  }
  // A held job moves when something outside it moves (a login, a rate window, the IST rollover),
  // so it is watched more slowly than one that is actively settling tasks.
  return state.group === 'active' ? 5_000 : 15_000
}

/** True when any job on a page can still move. Drives the list's own refetch cadence. */
export function anyJobLive(jobs: readonly JobRow[]): boolean {
  return jobs.some((job) => deriveJobState(job).isLive)
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const TONE_VARIANT: Record<StateTone, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  neutral: 'outline',
  running: 'default',
  attention: 'secondary',
  good: 'secondary',
  bad: 'destructive',
}

export function JobStateBadge({ job, className }: { job: Pick<JobRow, 'status'>; className?: string }) {
  const state = deriveJobState(job)
  return (
    <Badge variant={TONE_VARIANT[state.tone]} className={className} title={state.meaning}>
      {state.label}
    </Badge>
  )
}

function Counter({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'bad' | 'muted'
}) {
  return (
    <div className="flex min-w-16 flex-col gap-0.5">
      <span className="text-[0.65rem] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span
        className={cn(
          'text-sm tabular-nums',
          tone === 'bad' && value > 0 && 'text-destructive',
          tone === 'muted' && 'text-muted-foreground',
        )}
      >
        {formatInteger(value)}
      </span>
    </div>
  )
}

export interface JobProgressHeaderProps {
  job: JobRow
  /** The lifecycle buttons. Supplied by the screen, because only the screen owns the mutations. */
  actions?: ReactNode
  /** True when the broker token is good. Decides whether the parked job's remedy is offered. */
  brokerConnected?: boolean
  /** Shown beside the state, e.g. a live or reconnecting indicator. */
  children?: ReactNode
  className?: string
}

/**
 * The live progress block at the top of the job detail screen.
 *
 * Every number here comes from the job row in the query cache, which the SSE stream patches and
 * the screen's own query replaces. There is no second counter kept in component state, so there
 * is nothing that can be left behind when the stream drops.
 */
export function JobProgressHeader({
  job,
  actions,
  brokerConnected = true,
  children,
  className,
}: JobProgressHeaderProps) {
  const state = deriveJobState(job)
  const progress = jobProgress(job)

  return (
    <section className={cn('flex flex-col gap-3 border-b px-5 py-4', className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-heading text-base font-semibold tracking-tight">
              {humaniseCode(job.kind)}
            </h1>
            <JobStateBadge job={job} />
            {job.cancel_requested && !state.isFailed && state.isLive ? (
              <Badge variant="outline">Cancelling</Badge>
            ) : null}
            {children}
          </div>
          <p className="mt-0.5 font-mono text-xs text-muted-foreground">{job.job_id}</p>
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>

      <p className="max-w-3xl text-sm text-muted-foreground">{state.meaning}</p>

      {state.needsReauth && !brokerConnected ? (
        <p className="text-sm">
          <Link to="/settings" className="font-medium underline underline-offset-4">
            Log in to Fyers
          </Link>{' '}
          <span className="text-muted-foreground">
            and this job continues from exactly where it stopped. Nothing needs retrying.
          </span>
        </p>
      ) : null}

      {state.needsReauth && brokerConnected ? (
        <p className="text-sm text-muted-foreground">
          The broker token is valid again. Resume to put this job back in the queue.
        </p>
      ) : null}

      <div className="flex flex-col gap-1.5">
        <Progress value={progress.fraction * 100} />
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="tabular-nums">
            {formatInteger(progress.settled)} of {formatInteger(progress.total)} tasks settled,{' '}
            {progress.percentLabel}
          </span>
          <span className="tabular-nums">
            {progress.remaining > 0 ? 'Remaining ' + formatInteger(progress.remaining) : 'Nothing left to do'}
            {state.group === 'active' ? ', estimated ' + progress.etaLabel + ' left' : ''}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-3">
        <Counter label="Done" value={job.done_tasks} />
        <Counter label="Empty" value={job.empty_tasks} tone="muted" />
        <Counter label="Failed" value={job.failed_tasks} tone="bad" />
        <Counter label="Skipped" value={job.skipped_tasks} tone="muted" />
        <Counter label="Cancelled" value={job.cancelled_tasks} tone="muted" />
        <Counter label="Pending" value={job.pending_tasks} />
        <Counter label="Leased" value={job.leased_tasks} />
        <Counter label="Requests" value={job.requests_used} />
        <Counter label="Rows" value={job.rows_written} />
        <div className="flex min-w-24 flex-col gap-0.5">
          <span className="text-[0.65rem] uppercase tracking-wider text-muted-foreground">
            Throughput
          </span>
          <span className="text-sm tabular-nums">{progress.throughputLabel}</span>
        </div>
      </div>

      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <div className="flex gap-1.5">
          <dt>Created</dt>
          <dd className="text-foreground">{formatDateTime(job.created_at)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>Started</dt>
          <dd className="text-foreground">{formatDateTime(job.started_at)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>Finished</dt>
          <dd className="text-foreground">{formatDateTime(job.finished_at)}</dd>
        </div>
        {job.schedule_id ? (
          <div className="flex gap-1.5">
            <dt>Fired by</dt>
            <dd className="text-foreground">{job.schedule_id}</dd>
          </div>
        ) : null}
      </dl>

      {job.reason ? (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
          <span className="text-muted-foreground">Reason given: </span>
          {job.reason}
        </p>
      ) : null}
    </section>
  )
}

export default JobProgressHeader
