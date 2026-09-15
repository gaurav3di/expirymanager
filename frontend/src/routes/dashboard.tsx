import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { BudgetGauge } from '@/components/common/BudgetGauge'
import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import type { CoverageGrid } from '@/components/exports/ExportDialog'
import { apiErrorMessage } from '@/components/settings/BrokerPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, Budget, Storage } from '@/lib/api/types'
import {
  formatBytes,
  formatCompact,
  formatDateTime,
  formatDuration,
  formatInteger,
  formatRelative,
  humaniseCode,
  pluralise,
  safeRatio,
} from '@/lib/format'
import { describeCron } from '@/routes/schedules'
import type { ScheduleRow } from '@/routes/schedules'

// The first screen after a login, and the one that has to answer "what do I have, and what is
// happening" without a click.
//
// The order on the page is the order the questions get asked: is anything wrong, is anything
// running, what is on disk, and what will happen next on its own. Everything here is read only.
// Nothing on this screen spends a Fyers request, and the one action it offers is resuming work
// that is already paid for.

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/** The honesty fields on a job row are what this screen switches on, so they are named here
 *  rather than inferred from the status string. A job parked by the nightly logout is not a
 *  failure and must never be drawn as one. */
export interface JobSummary {
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
  requests_used: number
  rows_written: number
  throughput_per_minute: number | null
  eta_seconds: number | null
  reason: string | null
  state_group: string
  needs_reauth: boolean
  blocked_reason: string | null
  is_failed: boolean
  has_failures: boolean
  can_pause: boolean
  can_resume: boolean
  can_cancel: boolean
  can_retry_failed: boolean
}

export interface JobPage {
  items: JobSummary[]
  next_cursor: string | null
}

export interface UnderlyingRow {
  underlying_id: number
  fyers_symbol: string
  display_name: string
  exchange: string
  is_active: boolean
  mirrored: boolean
  expiry_count: number
  contract_count: number
  first_expiry: string | null
  last_expiry: string | null
  spot_bars: number
  spot_last_ts: string | null
}

export interface HealthRow {
  check_name: string
  status: string
  offending: number
  detail: string | null
  observed_at: string | null
}

export interface HealthResponse {
  rows: HealthRow[]
  last_maintenance: { ran_at: string; outcome: string; detail: string | null } | null
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export interface JobBuckets {
  /** Doing work right now. */
  running: JobSummary[]
  /** Started and stopped without finishing. Every one of these can be picked back up. */
  interrupted: JobSummary[]
  /** Finished, newest first, for the "what happened while I was away" line. */
  finished: JobSummary[]
}

export function bucketJobs(jobs: JobSummary[]): JobBuckets {
  const running: JobSummary[] = []
  const interrupted: JobSummary[] = []
  const finished: JobSummary[] = []
  for (const job of jobs) {
    if (job.state_group === 'active') {
      running.push(job)
    } else if (
      job.state_group === 'paused' ||
      job.state_group === 'blocked' ||
      job.state_group === 'deferred'
    ) {
      interrupted.push(job)
    } else if (job.state_group === 'terminal') {
      finished.push(job)
    }
  }
  return { running, interrupted, finished }
}

export type ResumeAction = 'sign_in' | 'resume' | 'retry_failed' | 'wait'

export interface Interruption {
  headline: string
  meaning: string
  action: ResumeAction
  actionLabel: string
}

/**
 * What stopped a job, and the one thing that starts it again.
 *
 * `needs_reauth` is checked before anything else because it is the common case and the only one
 * that is not a problem: the 03:00 IST logout parks running jobs on purpose, and they carry on
 * from the exact task after the next login. Pressing Resume on one of those would fail the auth
 * gate again, so this offers the login instead.
 */
export function describeInterruption(job: JobSummary): Interruption {
  if (job.needs_reauth || job.blocked_reason === 'authentication') {
    return {
      headline: 'Waiting for a Fyers login',
      meaning:
        'This job was parked, not failed. Nothing it had already downloaded was lost, and it ' +
        'picks up at the next task by itself once a token is stored.',
      action: 'sign_in',
      actionLabel: 'Sign in to Fyers',
    }
  }
  if (job.blocked_reason === 'rate_limit') {
    return {
      headline: 'Held back by the rate limiter',
      meaning:
        'The governor stopped the pipeline after per minute violations. It clears on its own; ' +
        'resuming before it does spends the same requests on the same refusal.',
      action: 'wait',
      actionLabel: 'Open the job',
    }
  }
  if (job.blocked_reason === 'budget' || job.state_group === 'deferred') {
    return {
      headline: 'Waiting for tomorrow',
      meaning:
        "The daily request budget is spent. This job starts on its own once the IST date rolls " +
        'and the new quota day opens.',
      action: 'wait',
      actionLabel: 'Open the job',
    }
  }
  if (job.state_group === 'paused') {
    return {
      headline: 'Paused',
      meaning:
        'Paused by hand. Every task it had finished stays finished, so resuming costs only what ' +
        'is left.',
      action: 'resume',
      actionLabel: 'Resume',
    }
  }
  if (job.can_retry_failed) {
    return {
      headline: pluralise(job.failed_tasks, 'task') + ' failed',
      meaning:
        'Retrying builds a child job holding only the failed tasks. Nothing that already ' +
        'succeeded is fetched again.',
      action: 'retry_failed',
      actionLabel: 'Retry the failed tasks',
    }
  }
  return {
    headline: humaniseCode(job.status),
    meaning: job.reason ?? '',
    action: 'wait',
    actionLabel: 'Open the job',
  }
}

/** Done over total, as a fraction. Empty, failed and skipped tasks are settled: a job whose
 *  tasks all came back empty is finished, not stuck at zero. */
export function settledFraction(job: JobSummary): number {
  const settled = job.done_tasks + job.empty_tasks + job.failed_tasks + job.skipped_tasks
  return safeRatio(settled, job.total_tasks)
}

// ---------------------------------------------------------------------------
// Coverage
// ---------------------------------------------------------------------------

export interface ResolutionCoverage {
  res_id: number
  fyers_code: string
  label: string
  contractsWithData: number
  contractsTotal: number
  rows: number
  chunksOk: number
  chunksEmpty: number
  chunksError: number
}

export interface GridSummary {
  rows: number
  expiriesWithData: number
  resolutions: ResolutionCoverage[]
}

/**
 * The coverage ledger for one underlying, rolled up per resolution.
 *
 * Rows are summed across every cell, which is safe because a cell is one expiry and one
 * resolution and a candle belongs to exactly one such pair. Contracts are summed only WITHIN a
 * resolution: the same contract appears once per resolution, so adding those columns across
 * resolutions would report more contracts than exist.
 */
export function summariseGrid(grid: CoverageGrid | undefined): GridSummary {
  if (!grid) {
    return { rows: 0, expiriesWithData: 0, resolutions: [] }
  }
  const byRes = new Map<number, ResolutionCoverage>()
  for (const resolution of grid.resolutions) {
    byRes.set(resolution.res_id, {
      res_id: resolution.res_id,
      fyers_code: resolution.fyers_code,
      label: resolution.label,
      contractsWithData: 0,
      contractsTotal: 0,
      rows: 0,
      chunksOk: 0,
      chunksEmpty: 0,
      chunksError: 0,
    })
  }
  let rows = 0
  const expiries = new Set<string>()
  for (const cell of grid.cells) {
    rows += cell.rows
    if (cell.rows > 0) {
      expiries.add(cell.expiry_date)
    }
    const entry = byRes.get(cell.res_id)
    if (!entry) {
      // A cell for a resolution the grid did not name as a column. The ledger is the authority
      // on what was fetched, so the cell is counted rather than dropped.
      continue
    }
    entry.contractsWithData += cell.contracts_with_data
    entry.contractsTotal += cell.contracts_total
    entry.rows += cell.rows
    entry.chunksOk += cell.chunks_ok
    entry.chunksEmpty += cell.chunks_empty
    entry.chunksError += cell.chunks_error
  }
  return { rows, expiriesWithData: expiries.size, resolutions: [...byRes.values()] }
}

// ---------------------------------------------------------------------------
// Schedules
// ---------------------------------------------------------------------------

export interface NextFire {
  schedule: ScheduleRow
  at: string
}

/** The enabled schedules that have a next fire, soonest first. A disabled schedule has no next
 *  fire and is not a plan. */
export function nextFires(schedules: ScheduleRow[], limit: number): NextFire[] {
  return schedules
    .filter(
      (schedule): schedule is ScheduleRow & { next_fire_at: string } =>
        schedule.enabled && typeof schedule.next_fire_at === 'string' && schedule.next_fire_at !== '',
    )
    .map((schedule) => ({ schedule, at: schedule.next_fire_at }))
    .sort((left, right) => (left.at < right.at ? -1 : left.at > right.at ? 1 : 0))
    .slice(0, limit)
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function Panel({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex min-w-0 flex-col rounded-lg border">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b px-3 py-2">
        <div className="min-w-0">
          <h2 className="text-sm font-medium tracking-tight">{title}</h2>
          {description ? (
            <p className="text-xs text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {actions}
      </header>
      <div className="min-w-0 p-3">{children}</div>
    </section>
  )
}

/**
 * Contracts that hold candles, against the contracts the expiry says exist.
 *
 * Deliberately not the shared CoverageBar. That component measures CHUNKS and says so in its
 * tooltip and in its accessible name, and the ledger cannot say how many chunks are still
 * missing, only how many contracts have no coverage row at all. Borrowing it here would put the
 * word chunks on a count of contracts, which is the kind of quiet mislabel this screen exists to
 * avoid. The palette is the same, so the two read as one family.
 */
function ContractsBar({
  covered,
  total,
  label,
}: {
  covered: number
  total: number
  label: string
}) {
  const fraction = safeRatio(covered, total)
  return (
    <div
      className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted"
      role="img"
      aria-label={
        label +
        ': ' +
        formatInteger(covered) +
        ' of ' +
        formatInteger(total) +
        ' contracts hold candles'
      }
    >
      {covered === 0 ? null : (
        <div
          className="bg-chart-5 dark:bg-chart-1"
          style={{ width: (fraction * 100).toFixed(4) + '%' }}
        />
      )}
    </div>
  )
}

function StatTile({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail?: ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col rounded-lg border px-3 py-2.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="mt-0.5 text-lg font-semibold tabular-nums tracking-tight">{value}</span>
      {detail ? <span className="text-xs text-muted-foreground">{detail}</span> : null}
    </div>
  )
}

const JOB_PAGE = 50
const NEXT_FIRE_COUNT = 6

export function DashboardRoute() {
  const client = useQueryClient()

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    staleTime: 60_000,
  })

  const budget = useQuery({
    queryKey: queryKeys.system.budget(),
    queryFn: () => api.get<Budget>('/system/budget'),
    retry: false,
  })

  const storage = useQuery({
    queryKey: queryKeys.system.storage(),
    queryFn: () => api.get<Storage>('/system/storage'),
    retry: false,
  })

  const health = useQuery({
    queryKey: queryKeys.system.health(),
    queryFn: () => api.get<HealthResponse>('/system/health'),
    retry: false,
  })

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({}),
    queryFn: () => api.get<UnderlyingRow[]>('/underlyings'),
    retry: false,
  })

  const jobs = useQuery({
    queryKey: queryKeys.jobs.list({ limit: JOB_PAGE }),
    queryFn: () => api.get<JobPage>('/jobs', { query: { limit: JOB_PAGE } }),
    retry: false,
  })

  const schedules = useQuery({
    queryKey: queryKeys.schedules.list(),
    queryFn: () => api.get<ScheduleRow[]>('/schedules'),
    retry: false,
  })

  const activeUnderlyings = useMemo(
    () => (underlyings.data ?? []).filter((row) => row.is_active),
    [underlyings.data],
  )

  const grids = useQueries({
    queries: activeUnderlyings.map((row) => ({
      queryKey: queryKeys.coverage.grid({ underlying_id: row.underlying_id }),
      queryFn: () =>
        api.get<CoverageGrid>('/coverage/grid', { query: { underlying_id: row.underlying_id } }),
      retry: false,
    })),
    combine: (results) => {
      const byId = new Map<number, CoverageGrid>()
      results.forEach((result, index) => {
        const row = activeUnderlyings[index]
        if (result.data && row) {
          byId.set(row.underlying_id, result.data)
        }
      })
      return byId
    },
  })

  const buckets = useMemo(() => bucketJobs(jobs.data?.items ?? []), [jobs.data])
  const fires = useMemo(() => nextFires(schedules.data ?? [], NEXT_FIRE_COUNT), [schedules.data])

  const failingChecks = useMemo(
    () => (health.data?.rows ?? []).filter((row) => row.status !== 'ok'),
    [health.data],
  )

  const resume = useMutation({
    mutationFn: (jobId: string) => api.post<unknown>('/jobs/' + jobId + '/resume'),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      toast.success('Resumed. It carries on from the task it stopped on.')
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'That job could not be resumed.')
    },
  })

  const retryFailed = useMutation({
    mutationFn: (jobId: string) =>
      api.post<{ job_id: string; total_tasks: number }>('/jobs/' + jobId + '/retry-failed'),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      toast.success(
        'Retrying ' + pluralise(result.total_tasks, 'task') + ' in a new job.',
      )
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'Those tasks could not be retried.')
    },
  })

  const totalContracts = (underlyings.data ?? []).reduce(
    (sum, row) => sum + row.contract_count,
    0,
  )
  const totalExpiries = (underlyings.data ?? []).reduce((sum, row) => sum + row.expiry_count, 0)

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Dashboard"
        description="What is in the store, what the pipeline is doing, and what it will do next on its own."
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              void client.invalidateQueries()
            }}
          >
            Refresh
          </Button>
        }
      />

      <div className="flex flex-col gap-4 px-5 py-4">
        {/* 1. Anything that needs a person. */}
        {buckets.interrupted.length > 0 ? (
          <Panel
            title="Picked up where it stopped"
            description="Work that started and stopped without finishing. None of it was thrown away."
          >
            <ul className="flex flex-col gap-2">
              {buckets.interrupted.map((job) => {
                const interruption = describeInterruption(job)
                return (
                  <li
                    key={job.job_id}
                    className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-md border px-3 py-2"
                  >
                    <div className="min-w-0 max-w-prose">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">{interruption.headline}</span>
                        <Badge variant="outline">{humaniseCode(job.kind)}</Badge>
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {formatInteger(
                            job.done_tasks + job.empty_tasks + job.failed_tasks + job.skipped_tasks,
                          )}{' '}
                          of {formatInteger(job.total_tasks)} tasks settled
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {interruption.meaning}
                      </p>
                      {/* The reason column often holds the same block code the sentence above
                          has just explained in words. Printing it again would put the backend's
                          vocabulary on screen for nothing. */}
                      {job.reason && job.reason !== job.blocked_reason ? (
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {humaniseCode(job.reason)}
                        </p>
                      ) : null}
                    </div>

                    <div className="flex shrink-0 items-center gap-1.5">
                      {interruption.action === 'sign_in' ? (
                        <Button size="sm" asChild>
                          <Link to="/settings">{interruption.actionLabel}</Link>
                        </Button>
                      ) : null}
                      {interruption.action === 'resume' ? (
                        <Button
                          size="sm"
                          disabled={resume.isPending}
                          onClick={() => resume.mutate(job.job_id)}
                        >
                          {interruption.actionLabel}
                        </Button>
                      ) : null}
                      {interruption.action === 'retry_failed' ? (
                        <Button
                          size="sm"
                          disabled={retryFailed.isPending}
                          onClick={() => retryFailed.mutate(job.job_id)}
                        >
                          {interruption.actionLabel}
                        </Button>
                      ) : null}
                      <Button size="sm" variant="ghost" asChild>
                        <Link to={'/jobs/' + job.job_id}>Open</Link>
                      </Button>
                    </div>
                  </li>
                )
              })}
            </ul>
          </Panel>
        ) : null}

        {failingChecks.length > 0 ? (
          <Panel
            title="Data health"
            description="Assertions the store runs against itself. A row here is a fact about the data, not a warning about the app."
          >
            <ul className="flex flex-col gap-1 text-xs">
              {failingChecks.map((row) => (
                <li key={row.check_name} className="flex flex-wrap gap-x-2">
                  <span className="font-medium">{humaniseCode(row.check_name)}</span>
                  {/* The backend's own detail already counts the offending rows. Printing the
                      count beside it says the same number twice. */}
                  <span className="text-destructive">
                    {row.detail ?? pluralise(row.offending, 'offending row')}
                  </span>
                </li>
              ))}
            </ul>
          </Panel>
        ) : null}

        {/* 2. The shape of what is here. */}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="Underlyings"
            value={formatInteger(activeUnderlyings.length)}
            detail={
              (underlyings.data ?? []).length === activeUnderlyings.length
                ? 'all active'
                : formatInteger((underlyings.data ?? []).length) + ' registered'
            }
          />
          <StatTile
            label="Contracts"
            value={formatCompact(totalContracts)}
            detail={pluralise(totalExpiries, 'expiry', 'expiries') + ' discovered'}
          />
          <StatTile
            label="Candles"
            value={formatCompact(storage.data?.candle_rows ?? 0)}
            detail={
              storage.data
                ? formatInteger(storage.data.candle_rows) + ' rows'
                : 'reading the store'
            }
          />
          <StatTile
            label="On disk"
            value={formatBytes(storage.data?.duckdb_bytes ?? 0)}
            detail={
              storage.data
                ? formatBytes(storage.data.free_disk_bytes) + ' free'
                : 'reading the store'
            }
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          {/* 3. What is happening right now. */}
          <div className="lg:col-span-2">
            <Panel
              title="Running now"
              description={
                buckets.running.length === 0
                  ? 'Nothing is downloading.'
                  : pluralise(buckets.running.length, 'job') + ' in flight.'
              }
              actions={
                <Button size="sm" variant="ghost" asChild>
                  <Link to="/jobs">All jobs</Link>
                </Button>
              }
            >
              {buckets.running.length === 0 ? (
                <EmptyState
                  className="border-0 py-6"
                  title="The pipeline is idle"
                  description={
                    buckets.finished.length > 0
                      ? 'The last job finished ' +
                        formatRelative(buckets.finished[0].finished_at) +
                        '. Start a download from the Expiries screen.'
                      : 'Nothing has run yet. Register an underlying, discover its expiries, then download them.'
                  }
                  action={
                    <Button size="sm" asChild>
                      <Link to="/expiries">Go to expiries</Link>
                    </Button>
                  }
                />
              ) : (
                <ul className="flex flex-col gap-3">
                  {buckets.running.map((job) => (
                    <li key={job.job_id} className="flex flex-col gap-1.5">
                      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={'/jobs/' + job.job_id}
                            className="text-sm font-medium underline-offset-4 hover:underline"
                          >
                            {humaniseCode(job.kind)}
                          </Link>
                          <Badge variant="outline">{humaniseCode(job.status)}</Badge>
                        </div>
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {formatInteger(
                            job.done_tasks +
                              job.empty_tasks +
                              job.failed_tasks +
                              job.skipped_tasks,
                          )}{' '}
                          of {formatInteger(job.total_tasks)}
                          {job.eta_seconds !== null
                            ? ', about ' + formatDuration(job.eta_seconds) + ' left'
                            : ''}
                        </span>
                      </div>
                      <Progress value={settledFraction(job) * 100} className="h-1.5" />
                      <div className="flex flex-wrap gap-x-4 text-xs text-muted-foreground tabular-nums">
                        <span>{formatInteger(job.rows_written)} rows written</span>
                        <span>{formatInteger(job.requests_used)} requests spent</span>
                        {job.throughput_per_minute !== null ? (
                          <span>
                            {formatInteger(Math.round(job.throughput_per_minute))} tasks a minute
                          </span>
                        ) : null}
                        {job.failed_tasks > 0 ? (
                          <span className="text-destructive">
                            {formatInteger(job.failed_tasks)} failed
                          </span>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>

          {/* 4. The two facts that decide whether the rest of the day works. */}
          <div className="flex flex-col gap-4">
            <Panel title="Broker">
              <dl className="flex flex-col gap-1 text-xs">
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-muted-foreground">Token</dt>
                  <dd>{humaniseCode(bootstrap.data?.token_state ?? 'none')}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-muted-foreground">Valid until</dt>
                  <dd className="tabular-nums">
                    {formatDateTime(bootstrap.data?.token_expires_at)}
                  </dd>
                </div>
              </dl>
              <p className="mt-2 max-w-prose text-xs text-muted-foreground">
                Fyers issues day tokens, and this app signs out on its own schedule overnight.
                Work that is running at the time is parked and resumes after the next login.
              </p>
            </Panel>

            <Panel title="Requests">
              <BudgetGauge budget={budget.data} variant="panel" />
            </Panel>
          </div>
        </div>

        {/* 5. What is in the store, per underlying. */}
        <Panel
          title="Coverage"
          description="Read from the coverage ledger, which records every window that was fetched and what came back."
          actions={
            <Button size="sm" variant="ghost" asChild>
              <Link to="/underlyings">Underlyings</Link>
            </Button>
          }
        >
          {underlyings.isPending ? (
            <p className="text-sm text-muted-foreground">Reading the catalogue.</p>
          ) : activeUnderlyings.length === 0 ? (
            <EmptyState
              className="border-0 py-6"
              title="No active underlyings"
              description="Register one and this fills in with what has been downloaded for it."
              action={
                <Button size="sm" asChild>
                  <Link to="/underlyings">Add an underlying</Link>
                </Button>
              }
            />
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {activeUnderlyings.map((row) => {
                const summary = summariseGrid(grids.get(row.underlying_id))
                return (
                  <div key={row.underlying_id} className="flex min-w-0 flex-col rounded-md border px-3 py-2.5">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                      <div className="min-w-0">
                        <span className="text-sm font-medium">{row.display_name}</span>
                        <span className="ml-2 font-mono text-xs text-muted-foreground">
                          {row.fyers_symbol}
                        </span>
                      </div>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {formatCompact(summary.rows)} candles
                      </span>
                    </div>

                    <p className="mt-0.5 text-xs text-muted-foreground tabular-nums">
                      {pluralise(row.expiry_count, 'expiry', 'expiries')},{' '}
                      {formatInteger(row.contract_count)} contracts
                      {row.first_expiry
                        ? ', ' + row.first_expiry + ' to ' + String(row.last_expiry)
                        : ''}
                    </p>

                    {!row.mirrored ? (
                      <p className="mt-1 text-xs text-destructive">
                        This underlying has no row in the candle store mirror, so every catalogue
                        join answers empty for it. Re-save it on the Underlyings screen to repair
                        the mirror.
                      </p>
                    ) : null}

                    {summary.resolutions.length === 0 ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Nothing downloaded yet.
                      </p>
                    ) : (
                      <ul className="mt-2 flex flex-col gap-2">
                        {summary.resolutions.map((resolution) => (
                          <li key={resolution.res_id} className="flex flex-col gap-1">
                            <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-xs">
                              <span className="font-medium">{resolution.label}</span>
                              <span className="text-muted-foreground tabular-nums">
                                {formatInteger(resolution.contractsWithData)} of{' '}
                                {formatInteger(resolution.contractsTotal)} contracts,{' '}
                                {formatCompact(resolution.rows)} candles
                              </span>
                            </div>
                            <ContractsBar
                              label={row.display_name + ' ' + resolution.label}
                              covered={resolution.contractsWithData}
                              total={resolution.contractsTotal}
                            />
                            {resolution.chunksError > 0 ? (
                              <span className="text-xs text-destructive tabular-nums">
                                {pluralise(resolution.chunksError, 'window')} came back as an
                                error and hold nothing
                              </span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </Panel>

        {/* 6. What happens next without anyone doing anything. */}
        <Panel
          title="Next on its own"
          description="The schedules that will fire next, soonest first."
          actions={
            <Button size="sm" variant="ghost" asChild>
              <Link to="/schedules">Schedules</Link>
            </Button>
          }
        >
          {fires.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No schedule is enabled with a next fire, so nothing will happen without you.
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {fires.map(({ schedule, at }) => (
                <li
                  key={schedule.schedule_id}
                  className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5"
                >
                  <div className="min-w-0">
                    <span className="text-sm">{schedule.name}</span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {describeCron(schedule.cron, schedule.timezone)}
                    </span>
                  </div>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    {formatRelative(at)}, {formatDateTime(at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  )
}

export default DashboardRoute
