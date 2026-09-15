import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { RunHistoryDrawer } from '@/components/schedules/RunHistoryDrawer'
import { ScheduleDialog } from '@/components/schedules/ScheduleDialog'
import { apiErrorMessage, isRouteMissing } from '@/components/settings/BrokerPanel'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatDateTime, formatRelative, humaniseCode } from '@/lib/format'

// The schedules screen.
//
// Everything a timer does here is ordinary and most of it is invisible, so the two things this
// screen exists for are: saying in plain words WHEN each schedule runs, and being honest about
// what happened when one did.
//
// Honesty has two specific shapes on this screen, and both of them are the difference between a
// green row and the truth:
//
//  1. The 03:00 IST logout is an EXPECTED daily event. It clears the broker token on purpose and
//     parks whatever was running as awaiting authentication, to be resumed from the exact task
//     after the next login. Rendering it as a failure, or rendering the skipped_needs_auth
//     outcomes that follow it as failures, teaches the user to ignore this screen.
//  2. Maintenance reports `completed` even when its own note says the trading day refresh
//     FAILED, or that it found duplicate rows or coverage mismatches. That note is the only
//     place those findings surface, so a run whose note carries one is not plain success and is
//     not drawn as one.

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/** One row of GET /api/v1/schedules. `description` is the registered action's own text, so what
 *  a schedule does is described by the code that does it rather than by a table here. */
export interface ScheduleRow {
  schedule_id: string
  name: string
  kind: string
  cron: string
  timezone: string
  enabled: boolean
  trading_days_only: boolean
  misfire_grace_seconds: number
  max_requests_per_run: number | null
  is_builtin: boolean
  params: Record<string, unknown>
  next_fire_at: string | null
  last_fired_at: string | null
  last_outcome: string | null
  last_job_id: string | null
  description: string | null
}

/** One row of GET /api/v1/schedules/{id}/runs. The free text field is `note`, not `detail`. */
export interface ScheduleRunRow {
  run_id: string
  schedule_id: string
  fired_at: string
  job_id: string | null
  outcome: string
  note: string | null
}

export interface RunNowResult {
  schedule_id: string
  run_id: string
  job_id: string | null
  outcome: string
  note: string | null
}

// ---------------------------------------------------------------------------
// Cron, in words
// ---------------------------------------------------------------------------

const DAY_NAMES = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
]

/** Expands the forms this app's own crons actually use: a number, a comma list, and a hyphen
 *  range. Anything else (a step, a wildcard, a name) returns null, and the caller falls back to
 *  printing the expression rather than guessing at it. */
function expandField(field: string, min: number, max: number): number[] | null {
  const values: number[] = []
  for (const part of field.split(',')) {
    const range = /^(\d{1,2})-(\d{1,2})$/.exec(part)
    if (range) {
      const start = Number(range[1])
      const end = Number(range[2])
      if (start > end) {
        return null
      }
      for (let value = start; value <= end; value += 1) {
        values.push(value)
      }
      continue
    }
    if (!/^\d{1,2}$/.test(part)) {
      return null
    }
    values.push(Number(part))
  }
  if (values.length === 0) {
    return null
  }
  for (const value of values) {
    if (value < min || value > max) {
      return null
    }
  }
  return [...new Set(values)].sort((left, right) => left - right)
}

function twoDigits(value: number): string {
  return value < 10 ? '0' + String(value) : String(value)
}

/** "09:00, 15:00 and 18:30". Plain words, no serial comma games, no glyphs. */
function joinList(items: string[]): string {
  if (items.length <= 1) {
    return items[0] ?? ''
  }
  return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1]
}

function describeDays(dayOfMonth: string, dayOfWeek: string): string | null {
  const everyDayOfMonth = dayOfMonth === '*'
  const everyDayOfWeek = dayOfWeek === '*'

  if (everyDayOfMonth && everyDayOfWeek) {
    return 'Every day'
  }

  if (everyDayOfMonth) {
    const days = expandField(dayOfWeek, 0, 7)
    if (!days) {
      return null
    }
    // Cron accepts 7 as Sunday as well as 0. Both name the same day, so they collapse here
    // rather than rendering "Sunday and Sunday".
    const names = [...new Set(days.map((day) => DAY_NAMES[day % 7]))]
    if (names.length === 5 && !names.includes('Saturday') && !names.includes('Sunday')) {
      // The exchange week. Said as a span because that is how a trader reads it, and because
      // this app never assumes a weekend is closed anywhere else: this is a description of the
      // cron expression, not a claim about the market.
      return 'Monday to Friday'
    }
    return names.length === 1 ? 'Every ' + names[0] : joinList(names)
  }

  if (everyDayOfWeek) {
    const days = expandField(dayOfMonth, 1, 31)
    if (!days) {
      return null
    }
    return days.length === 1
      ? 'Day ' + String(days[0]) + ' of the month'
      : 'Days ' + joinList(days.map(String)) + ' of the month'
  }

  return null
}

function describeTime(minute: string, hour: string): string | null {
  const everyHour = hour === '*'
  const step = /^\*\/(\d{1,2})$/.exec(minute)

  if (step) {
    const every = Number(step[1])
    if (every < 1 || every > 59) {
      return null
    }
    const span = every === 1 ? 'every minute' : 'every ' + String(every) + ' minutes'
    if (everyHour) {
      return span
    }
    const hours = expandField(hour, 0, 23)
    return hours
      ? span + ' during ' + joinList(hours.map((value) => twoDigits(value) + ':00'))
      : null
  }

  if (minute === '*') {
    return everyHour ? 'every minute' : null
  }

  const minutes = expandField(minute, 0, 59)
  if (!minutes) {
    return null
  }

  if (everyHour) {
    return minutes.length === 1 && minutes[0] === 0
      ? 'every hour, on the hour'
      : 'every hour at minute ' + joinList(minutes.map(String))
  }

  const hours = expandField(hour, 0, 23)
  if (!hours) {
    return null
  }

  const clocks: string[] = []
  for (const hourValue of hours) {
    for (const minuteValue of minutes) {
      clocks.push(twoDigits(hourValue) + ':' + twoDigits(minuteValue))
    }
  }
  return 'at ' + joinList(clocks)
}

/**
 * A five field cron expression in plain English.
 *
 * Returns the expression itself when it uses a form this does not read. A wrong sentence about
 * when a job runs is worse than the expression the scheduler is actually holding, so there is no
 * partial guess anywhere in here.
 */
export function describeCron(expression: string, timezone?: string): string {
  const fields = String(expression ?? '').trim().split(/\s+/)
  if (fields.length !== 5) {
    return String(expression ?? '')
  }
  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields
  if (month !== '*') {
    return expression
  }

  const days = describeDays(dayOfMonth, dayOfWeek)
  const time = describeTime(minute, hour)
  if (!days || !time) {
    return expression
  }

  const zone = !timezone ? '' : timezone === 'Asia/Kolkata' ? ' IST' : ' ' + timezone

  // "Every day every hour, on the hour" says the same thing twice. When the time phrase already
  // covers every day, it becomes the sentence on its own.
  const sentence =
    days === 'Every day' && time.startsWith('every ')
      ? time.charAt(0).toUpperCase() + time.slice(1)
      : days + ' ' + time

  return sentence + zone
}

// ---------------------------------------------------------------------------
// Outcomes, and the findings hiding inside a successful note
// ---------------------------------------------------------------------------

export type RunTone = 'ok' | 'pending' | 'skipped' | 'attention' | 'error'

/** Segments of a run note that report something that went wrong, even though the run itself
 *  reported completed.
 *
 *  Maintenance writes its note as "wal 1024 to 0 bytes; 250 trading days derived from spot bars;
 *  0 health checks with rows; 0 duplicate groups; 0 coverage mismatches; 12 rate events pruned",
 *  and on a bad day it writes "trading day refresh FAILED: OSError" into the same list. The
 *  counted segments are only interesting when the count is not zero, and the FAILED segment is
 *  always interesting. Those segments are returned verbatim: the note is the report, and
 *  rewording it here would put a second vocabulary in front of the user. */
export function concerningNoteParts(note: string | null | undefined): string[] {
  if (!note) {
    return []
  }
  const parts: string[] = []
  for (const raw of note.split(';')) {
    const part = raw.trim()
    if (!part) {
      continue
    }
    if (/\b(failed|failure|error|refused|could not|cannot)\b/i.test(part)) {
      parts.push(part)
      continue
    }
    const counted = /^(\d+)\s+(.+)$/.exec(part)
    if (
      counted &&
      Number(counted[1]) > 0 &&
      /(health checks with rows|duplicate groups|coverage mismatches|orphan|mismatch)/i.test(
        counted[2],
      )
    ) {
      parts.push(part)
    }
  }
  return parts
}

export interface RunDescription {
  tone: RunTone
  label: string
  /** One sentence of orientation. Never the raw note: the note is rendered separately and in
   *  full, so this says what the outcome means rather than repeating it. */
  meaning: string
  /** Note segments that contradict a plain success. Empty for a genuinely clean run. */
  findings: string[]
}

/**
 * How one run reads.
 *
 * `kind` matters for exactly one case: the scheduled logout clears the token on purpose, so its
 * completion is not the same event as a download schedule completing, and the sentence says so.
 */
export function describeRun(
  outcome: string,
  note: string | null | undefined,
  kind?: string,
): RunDescription {
  const findings = concerningNoteParts(note)

  if (outcome === 'error') {
    return {
      tone: 'error',
      label: 'Error',
      meaning: 'The fire itself failed. Nothing was queued.',
      findings,
    }
  }

  if (outcome === 'enqueued') {
    return {
      tone: 'pending',
      label: 'Queued work',
      meaning: 'The fire created a job. Its progress is on the Jobs screen.',
      findings,
    }
  }

  if (outcome === 'completed') {
    if (kind === 'token_logout') {
      return {
        tone: 'ok',
        label: 'Completed',
        meaning:
          'The broker token was cleared as scheduled. Anything running was parked awaiting ' +
          'authentication and resumes after the next login.',
        findings,
      }
    }
    if (findings.length > 0) {
      // The run reported success and its own note contradicts it. Drawing this as a plain
      // success is how a failed trading day refresh stays invisible until the planner starts
      // guessing at session hours.
      return {
        tone: 'attention',
        label: 'Completed with findings',
        meaning: 'The run finished, and it reported something that needs looking at.',
        findings,
      }
    }
    return {
      tone: 'ok',
      label: 'Completed',
      meaning: 'The run finished with nothing to report.',
      findings,
    }
  }

  if (outcome.startsWith('skipped_')) {
    const reason = outcome.slice('skipped_'.length)
    const meanings: Record<string, string> = {
      disabled: 'The schedule was switched off when the timer reached it.',
      holiday: 'Not a trading day for this schedule, so there was nothing to ask for.',
      needs_auth:
        'There was no usable Fyers token. The daily logout clears the token every night, so ' +
        'this is the normal outcome for a fire before the next sign in. Nothing was lost and ' +
        'nothing was spent.',
      blocked: 'The pipeline was stopped or rate limited, so the fire did not queue work.',
      budget: 'The daily request budget was already spent. The next fire after the day rolls will run.',
    }
    return {
      tone: 'skipped',
      label: 'Skipped, ' + reason.replace(/_/g, ' '),
      meaning: meanings[reason] ?? 'The fire was skipped.',
      findings,
    }
  }

  return { tone: 'pending', label: humaniseCode(outcome), meaning: '', findings }
}

const TONE_VARIANT: Record<RunTone, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  pending: 'outline',
  skipped: 'outline',
  attention: 'default',
  error: 'destructive',
}

export function OutcomeBadge({ description }: { description: RunDescription }) {
  return <Badge variant={TONE_VARIANT[description.tone]}>{description.label}</Badge>
}

// ---------------------------------------------------------------------------
// The screen
// ---------------------------------------------------------------------------

/** The kind whose whole purpose is to take the token away. Named once. */
export const LOGOUT_KIND = 'token_logout'

export function isScheduledLogout(row: ScheduleRow): boolean {
  return row.kind === LOGOUT_KIND
}

export function SchedulesRoute() {
  const client = useQueryClient()
  const [historyFor, setHistoryFor] = useState<ScheduleRow | null>(null)
  const [editing, setEditing] = useState<ScheduleRow | null>(null)
  const [creating, setCreating] = useState(false)

  const schedules = useQuery({
    queryKey: queryKeys.schedules.list(),
    queryFn: () => api.get<ScheduleRow[]>('/schedules'),
    retry: false,
  })

  const rows = useMemo(() => schedules.data ?? [], [schedules.data])
  const logoutRow = rows.find(isScheduledLogout) ?? null

  // The schedule row carries `last_outcome` but no note, and the note is the only place
  // maintenance reports a failed trading day refresh or a coverage mismatch while still
  // reporting the run as completed. So the newest run of every schedule that has ever fired is
  // read here, one small row each, rather than leaving that finding invisible until someone
  // opens the history drawer.
  const firedIds = useMemo(
    () => rows.filter((row) => row.last_fired_at).map((row) => row.schedule_id),
    [rows],
  )

  const lastRunById = useQueries({
    queries: firedIds.map((scheduleId) => ({
      queryKey: queryKeys.schedules.runs(scheduleId, { limit: 1 }),
      queryFn: () =>
        api.get<ScheduleRunRow[]>('/schedules/' + scheduleId + '/runs', {
          query: { limit: 1 },
        }),
      retry: false,
    })),
    combine: (results) => {
      const byId = new Map<string, ScheduleRunRow>()
      results.forEach((result, index) => {
        const run = result.data?.[0]
        const scheduleId = firedIds[index]
        if (run && scheduleId) {
          byId.set(scheduleId, run)
        }
      })
      return byId
    },
  })

  const needAttention = useMemo(
    () =>
      rows
        .map((row) => ({ row, run: lastRunById.get(row.schedule_id) }))
        .filter(
          (entry): entry is { row: ScheduleRow; run: ScheduleRunRow } =>
            entry.run !== undefined &&
            describeRun(entry.run.outcome, entry.run.note, entry.row.kind).findings.length > 0,
        ),
    [rows, lastRunById],
  )

  const refresh = () => {
    void client.invalidateQueries({ queryKey: queryKeys.schedules.all() })
  }

  const toggle = useMutation({
    mutationFn: (input: { scheduleId: string; enabled: boolean }) =>
      api.patch<ScheduleRow>('/schedules/' + input.scheduleId, {
        body: { enabled: input.enabled },
      }),
    onSuccess: (row) => {
      toast.success(
        row.enabled
          ? row.name + ' is on. Next fire ' + (row.next_fire_at ? formatRelative(row.next_fire_at) : 'not scheduled') + '.'
          : row.name + ' is off. The row stays here and can be switched back on.',
      )
      refresh()
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'That schedule could not be changed.')
    },
  })

  const runNow = useMutation({
    mutationFn: (input: { scheduleId: string; kind: string }) =>
      api
        .post<RunNowResult>('/schedules/' + input.scheduleId + '/run-now')
        .then((result) => ({ result, kind: input.kind })),
    onSuccess: ({ result, kind }) => {
      const described = describeRun(result.outcome, result.note, kind)
      const detail = described.meaning + (result.note ? ' ' + result.note : '')
      if (described.tone === 'error') {
        toast.error(described.label + '. ' + detail)
      } else if (described.tone === 'attention') {
        toast.warning(described.label + '. ' + detail)
      } else {
        toast.success(described.label + '. ' + detail)
      }
      refresh()
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'That schedule could not be fired.')
    },
  })

  const remove = useMutation({
    mutationFn: (scheduleId: string) => api.delete<void>('/schedules/' + scheduleId),
    onSuccess: () => {
      toast.success('Schedule deleted.')
      refresh()
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'That schedule could not be deleted.')
    },
  })

  if (schedules.isError) {
    return (
      <div className="flex min-h-full flex-col">
        <PageHeader title="Schedules" />
        <div className="px-5 py-4">
          <EmptyState
            title={
              isRouteMissing(schedules.error)
                ? 'Schedules are not wired up yet'
                : 'Cannot read the schedules'
            }
            description={
              isRouteMissing(schedules.error)
                ? 'This build does not serve the schedules route yet. Downloads started by hand are unaffected.'
                : (apiErrorMessage(schedules.error) ?? 'The backend returned an unexpected response.')
            }
            action={
              <Button size="sm" variant="outline" onClick={() => void schedules.refetch()}>
                Retry
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Schedules"
        description="What the app does on its own, when it does it, and what happened last time."
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => void schedules.refetch()}>
              Refresh
            </Button>
            <Button size="sm" onClick={() => setCreating(true)}>
              New schedule
            </Button>
          </>
        }
      />

      <div className="flex flex-col gap-4 px-5 py-4">
        {needAttention.length > 0 ? (
          <div
            role="alert"
            className="max-w-prose rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5"
          >
            <p className="text-sm font-medium">
              A run reported success and then reported a problem in the same breath
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              These are the parts of the last run note that contradict a clean finish. They are
              printed exactly as the run recorded them.
            </p>
            <ul className="mt-1.5 flex flex-col gap-1 text-xs">
              {needAttention.map(({ row, run }) => (
                <li key={row.schedule_id}>
                  <span className="font-medium">{row.name}</span>
                  <span className="text-muted-foreground">
                    {' '}
                    {formatDateTime(run.fired_at)}
                  </span>
                  <ul className="mt-0.5 flex flex-col gap-0.5 pl-3">
                    {describeRun(run.outcome, run.note, row.kind).findings.map((finding) => (
                      <li key={finding}>{finding}</li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {logoutRow ? (
          <div
            role="note"
            className="max-w-prose rounded-lg border bg-muted/40 px-3 py-2.5"
          >
            <p className="text-sm font-medium">
              {describeCron(logoutRow.cron, logoutRow.timezone)}, this app signs out of Fyers
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              That is an expected daily event and not an error. Fyers tokens are day tokens, so
              the token is cleared on purpose. A download that is running at the time is parked
              as awaiting authentication, never failed: after the next sign in it resumes from
              the exact task it stopped on. Until then, the schedules that need the broker report
              their fires as skipped, which is also not an error.
            </p>
          </div>
        ) : null}

        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-8 text-xs font-medium">Schedule</TableHead>
                <TableHead className="h-8 text-xs font-medium">When</TableHead>
                <TableHead className="h-8 text-xs font-medium">Next fire</TableHead>
                <TableHead className="h-8 text-xs font-medium">Last fire</TableHead>
                <TableHead className="h-8 text-xs font-medium">On</TableHead>
                <TableHead className="h-8 text-right text-xs font-medium">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const run = lastRunById.get(row.schedule_id)
                // The run row is the record of what happened; the schedule column is a copy of
                // it. Where both exist the run wins, because it carries the note.
                const lastRun = run
                  ? describeRun(run.outcome, run.note, row.kind)
                  : row.last_outcome
                    ? describeRun(row.last_outcome, null, row.kind)
                    : null
                return (
                  <TableRow key={row.schedule_id}>
                    <TableCell className="py-2 align-top">
                      <div className="flex flex-col gap-0.5">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm font-medium">{row.name}</span>
                          {row.is_builtin ? <Badge variant="outline">Built in</Badge> : null}
                          {isScheduledLogout(row) ? (
                            <Badge variant="outline">Expected daily event</Badge>
                          ) : null}
                        </div>
                        <span className="max-w-prose text-xs text-muted-foreground">
                          {row.description ?? humaniseCode(row.kind)}
                        </span>
                        <span className="font-mono text-[0.65rem] text-muted-foreground">
                          {row.kind}
                          {row.trading_days_only ? ', trading days only' : ''}
                          {row.max_requests_per_run
                            ? ', at most ' + String(row.max_requests_per_run) + ' requests a run'
                            : ''}
                        </span>
                      </div>
                    </TableCell>

                    <TableCell className="py-2 align-top">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-sm">{describeCron(row.cron, row.timezone)}</span>
                        <span className="font-mono text-[0.65rem] text-muted-foreground">
                          {row.cron}
                        </span>
                      </div>
                    </TableCell>

                    <TableCell className="py-2 align-top text-sm">
                      {row.enabled && row.next_fire_at ? (
                        <div className="flex flex-col gap-0.5">
                          <span>{formatRelative(row.next_fire_at)}</span>
                          <span className="text-xs text-muted-foreground">
                            {formatDateTime(row.next_fire_at)}
                          </span>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">
                          {row.enabled ? 'Not scheduled' : 'Switched off'}
                        </span>
                      )}
                    </TableCell>

                    <TableCell className="py-2 align-top text-sm">
                      {row.last_fired_at ? (
                        <div className="flex flex-col gap-1">
                          {/* The timestamp comes from the same place as the badge beside it.
                              Reading one from the run and the other from the schedule column
                              would label an outcome with a different fire's time. */}
                          <span className="text-xs text-muted-foreground">
                            {formatDateTime(run?.fired_at ?? row.last_fired_at)}
                          </span>
                          {lastRun ? (
                            <div className="flex flex-wrap items-center gap-1.5">
                              <OutcomeBadge description={lastRun} />
                              {(run?.job_id ?? row.last_job_id) ? (
                                <Link
                                  className="text-xs underline underline-offset-4"
                                  to={'/jobs/' + String(run?.job_id ?? row.last_job_id)}
                                >
                                  Job
                                </Link>
                              ) : null}
                            </div>
                          ) : null}
                          {lastRun && lastRun.findings.length > 0 ? (
                            <ul className="flex max-w-prose flex-col gap-0.5 text-xs text-destructive">
                              {lastRun.findings.map((finding) => (
                                <li key={finding}>{finding}</li>
                              ))}
                            </ul>
                          ) : null}
                        </div>
                      ) : (
                        <span className="text-muted-foreground">Never</span>
                      )}
                    </TableCell>

                    <TableCell className="py-2 align-top">
                      <Switch
                        checked={row.enabled}
                        aria-label={'Enable ' + row.name}
                        disabled={toggle.isPending}
                        onCheckedChange={(next) =>
                          toggle.mutate({ scheduleId: row.schedule_id, enabled: next })
                        }
                      />
                    </TableCell>

                    <TableCell className="py-2 align-top">
                      <div className="flex flex-wrap items-center justify-end gap-1.5">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={runNow.isPending}
                          onClick={() =>
                            runNow.mutate({ scheduleId: row.schedule_id, kind: row.kind })
                          }
                        >
                          Run now
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setHistoryFor(row)}
                        >
                          History
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(row)}>
                          Edit
                        </Button>
                        {row.is_builtin ? null : (
                          <AlertDialog>
                            <AlertDialogTrigger asChild>
                              <Button size="sm" variant="ghost">
                                Delete
                              </Button>
                            </AlertDialogTrigger>
                            <AlertDialogContent>
                              <AlertDialogHeader>
                                <AlertDialogTitle>Delete {row.name}?</AlertDialogTitle>
                                <AlertDialogDescription>
                                  The schedule and its run history go. Jobs it already created
                                  stay where they are.
                                </AlertDialogDescription>
                              </AlertDialogHeader>
                              <AlertDialogFooter>
                                <AlertDialogCancel>Keep it</AlertDialogCancel>
                                <AlertDialogAction
                                  onClick={() => remove.mutate(row.schedule_id)}
                                >
                                  Delete
                                </AlertDialogAction>
                              </AlertDialogFooter>
                            </AlertDialogContent>
                          </AlertDialog>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>

          {!schedules.isPending && rows.length === 0 ? (
            <EmptyState
              className="rounded-none border-0"
              title="No schedules"
              description="The built in schedules are seeded on first run. An empty table means the migration that seeds them has not run against this data directory."
            />
          ) : null}
          {schedules.isPending ? (
            <p className="px-3 py-4 text-sm text-muted-foreground">Reading the schedules.</p>
          ) : null}
        </div>
      </div>

      <RunHistoryDrawer
        schedule={historyFor}
        onOpenChange={(open) => {
          if (!open) {
            setHistoryFor(null)
          }
        }}
      />

      <ScheduleDialog
        open={creating}
        onOpenChange={setCreating}
        schedule={null}
        kinds={[...new Set(rows.map((row) => row.kind))].sort()}
      />

      <ScheduleDialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEditing(null)
          }
        }}
        schedule={editing}
        kinds={[...new Set(rows.map((row) => row.kind))].sort()}
      />
    </div>
  )
}

export default SchedulesRoute
