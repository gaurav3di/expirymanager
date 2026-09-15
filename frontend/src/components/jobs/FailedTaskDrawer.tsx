import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import type { JobTaskRow } from '@/components/jobs/TaskTable'
import { TaskStateBadge, describeTaskState, taskSubject, taskWindow } from '@/components/jobs/TaskTable'
import {
  EMPTY_VALUE,
  formatBytes,
  formatDateTime,
  formatInteger,
  formatLatency,
  humaniseCode,
} from '@/lib/format'

// The drill-through: one task, everything known about it, and why it ended the way it did.
//
// Opened from any row, not only a failed one, because "why is this empty" and "why was this
// skipped" are asked as often as "why did this fail" and all three are answered by the same set
// of stored facts.
//
// What is NOT here: the raw body. The route returns `has_raw_body` and never the path, so this
// panel reports that a body was captured and where to look for it conceptually, and offers no
// control that would ask the server to read a file by path.

export type TaskErrorKind = 'none' | 'auth' | 'rate_limit' | 'fatal' | 'transient' | 'unknown'

export interface TaskErrorReading {
  kind: TaskErrorKind
  headline: string
  explanation: string
  /** True when retrying this task could plausibly produce a different answer. */
  retryWorthwhile: boolean
}

/**
 * The vendor codes documented in the Fyers reference section 4, as sentences.
 *
 * The retry class beside each one is the same class the pipeline used when it decided whether to
 * spend another attempt, and job-state.test.ts reads brokers/fyers/errors.py off disk to assert
 * these agree. A screen that said "try again" about a code the pipeline classified as fatal
 * would be inviting the user to spend requests on a guaranteed refusal.
 */
const CODE_READINGS: Record<string, { kind: TaskErrorKind; text: string }> = {
  '-8': { kind: 'auth', text: 'The broker token had expired when this request was made.' },
  '-15': {
    kind: 'auth',
    text: 'The broker token was rejected as invalid, which needs a fresh login rather than a wait.',
  },
  '-16': { kind: 'auth', text: 'Fyers could not authenticate the token on this request.' },
  '-17': { kind: 'auth', text: 'The broker token was invalid or expired.' },
  '-50': { kind: 'fatal', text: 'Fyers rejected one or more parameters of this request.' },
  '-51': { kind: 'fatal', text: 'Fyers reported an invalid order id.' },
  '-53': { kind: 'fatal', text: 'Fyers reported an invalid position id.' },
  '-99': { kind: 'fatal', text: 'Fyers rejected the request outright.' },
  '-300': {
    kind: 'fatal',
    text:
      'Fyers does not know this symbol. For an expired contract that usually means the symbol ' +
      'was built from a master row the historical endpoints do not serve.',
  },
  '-352': { kind: 'fatal', text: 'Fyers rejected the app id on this request.' },
  '-429': { kind: 'rate_limit', text: 'Fyers refused this request as over the rate limit.' },
  '400': { kind: 'fatal', text: 'Fyers rejected the input of this request.' },
}

const HTTP_READINGS: Record<number, { kind: TaskErrorKind; text: string }> = {
  400: { kind: 'fatal', text: 'The vendor rejected the request as malformed.' },
  401: { kind: 'auth', text: 'The vendor refused the request as unauthenticated.' },
  403: { kind: 'fatal', text: 'The vendor refused the request.' },
  429: { kind: 'rate_limit', text: 'The vendor refused this request as over the rate limit.' },
  500: { kind: 'transient', text: 'The vendor failed on its own side.' },
  502: { kind: 'transient', text: 'The vendor gateway failed on its own side.' },
  503: { kind: 'transient', text: 'The vendor was unavailable.' },
  504: { kind: 'transient', text: 'The vendor timed out.' },
}

const HEADLINES: Record<TaskErrorKind, string> = {
  none: 'No error recorded',
  auth: 'The broker token was not accepted',
  rate_limit: 'The vendor refused this as over the limit',
  fatal: 'The vendor refused this request',
  transient: 'The vendor failed and every attempt was used',
  unknown: 'An error this build does not have a sentence for',
}

const REMEDIES: Record<TaskErrorKind, string> = {
  none: '',
  auth:
    'Log in to Fyers again. Tasks parked behind a dead token resume by themselves; a task that ' +
    'already exhausted its attempts needs Retry failed tasks once the token is back.',
  rate_limit:
    'Nothing to fix. The governor slows down by itself and Retry failed tasks refetches exactly ' +
    'these windows.',
  fatal: 'Retrying spends a request to be refused again. The stored error is the whole answer.',
  transient: 'Retry failed tasks is worth trying. The failure was on the vendor side.',
  unknown: 'The stored code and message below are everything the pipeline recorded.',
}

const RETRY_WORTHWHILE: Record<TaskErrorKind, boolean> = {
  none: false,
  auth: true,
  rate_limit: true,
  fatal: false,
  transient: true,
  unknown: true,
}

/**
 * Why this task ended the way it did.
 *
 * The error code is preferred over the HTTP status because a Fyers envelope carries a 200 with a
 * negative code inside it more often than it carries a real HTTP failure, so reading the status
 * first would classify most vendor refusals as success.
 */
export function classifyTaskError(
  task: Pick<JobTaskRow, 'state' | 'error_code' | 'error_message' | 'http_status'>,
): TaskErrorReading {
  if (task.state !== 'failed' && !task.error_code && !task.error_message) {
    return {
      kind: 'none',
      headline: HEADLINES.none,
      explanation: describeTaskState(task.state).meaning,
      retryWorthwhile: false,
    }
  }

  const code = task.error_code ?? ''
  const byCode = CODE_READINGS[code]
  const byHttp = task.http_status === null ? undefined : HTTP_READINGS[task.http_status]
  const reading = byCode ?? byHttp

  if (!reading) {
    return {
      kind: 'unknown',
      headline: HEADLINES.unknown,
      explanation: REMEDIES.unknown,
      retryWorthwhile: RETRY_WORTHWHILE.unknown,
    }
  }

  return {
    kind: reading.kind,
    headline: HEADLINES[reading.kind],
    explanation: reading.text + ' ' + REMEDIES[reading.kind],
    retryWorthwhile: RETRY_WORTHWHILE[reading.kind],
  }
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border/50 py-1 last:border-0">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right text-xs tabular-nums">{value}</dd>
    </div>
  )
}

/** The scrubbed request parameters, flattened for reading. Values arrive already redacted. */
function paramLines(value: unknown): Array<[string, string]> {
  if (value === null || value === undefined) {
    return []
  }
  if (typeof value !== 'object' || Array.isArray(value)) {
    return [['value', String(value)]]
  }
  return Object.entries(value as Record<string, unknown>).map(([key, item]) => [
    key,
    typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item),
  ])
}

export interface FailedTaskDrawerProps {
  task: JobTaskRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Offered only when the parent job actually has failed tasks to retry. */
  onRetryFailed?: () => void
  retryPending?: boolean
}

export function FailedTaskDrawer({
  task,
  open,
  onOpenChange,
  onRetryFailed,
  retryPending = false,
}: FailedTaskDrawerProps) {
  if (!task) {
    return null
  }

  const stateReading = describeTaskState(task.state)
  const error = classifyTaskError(task)
  const params = paramLines(task.request_params_json)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-md">
        <SheetHeader>
          <SheetTitle className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm">{taskSubject(task)}</span>
            <TaskStateBadge state={task.state} />
          </SheetTitle>
          <SheetDescription>{stateReading.meaning}</SheetDescription>
        </SheetHeader>

        {/* Its own scroll region so a long parameter blob never drops a platform scrollbar
            onto the panel. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-2">
          {error.kind !== 'none' ? (
            <section className="mb-4 rounded-md border bg-muted/40 p-3">
              <p className="text-sm font-medium">{error.headline}</p>
              <p className="mt-1 text-xs text-muted-foreground">{error.explanation}</p>
              {task.error_message ? (
                <p className="mt-2 break-words font-mono text-xs">{task.error_message}</p>
              ) : null}
            </section>
          ) : null}

          <dl className="flex flex-col">
            <Row label="Task" value={String(task.task_id)} />
            <Row label="Sequence" value={formatInteger(task.seq)} />
            <Row label="Kind" value={humaniseCode(task.kind)} />
            <Row label="Resolution" value={task.resolution ?? EMPTY_VALUE} />
            <Row label="Window" value={taskWindow(task)} />
            <Row
              label="Attempt"
              value={String(task.attempt) + ' of ' + String(task.max_attempts)}
            />
            <Row label="Error code" value={task.error_code ?? EMPTY_VALUE} />
            <Row
              label="HTTP status"
              value={task.http_status === null ? EMPTY_VALUE : String(task.http_status)}
            />
            <Row label="Rows written" value={formatInteger(task.row_count)} />
            <Row label="First candle" value={task.first_ts ?? EMPTY_VALUE} />
            <Row label="Last candle" value={task.last_ts ?? EMPTY_VALUE} />
            <Row label="Latency" value={formatLatency(task.latency_ms)} />
            <Row label="Response size" value={formatBytes(task.response_bytes)} />
            <Row label="Contract" value={task.contract_id === null ? EMPTY_VALUE : String(task.contract_id)} />
            <Row label="Open interest requested" value={task.include_oi ? 'yes' : 'no'} />
            <Row label="Started" value={formatDateTime(task.started_at)} />
            <Row label="Finished" value={formatDateTime(task.finished_at)} />
            <Row
              label="Raw body kept"
              value={task.has_raw_body ? 'yes, in the data directory' : 'no'}
            />
          </dl>

          {params.length > 0 ? (
            <section className="mt-4">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">
                Request parameters
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Returned with any token-shaped value already replaced by the backend.
              </p>
              <dl className="mt-2 flex flex-col">
                {params.map(([key, value]) => (
                  <Row key={key} label={key} value={value} />
                ))}
              </dl>
            </section>
          ) : null}
        </div>

        <SheetFooter>
          {onRetryFailed && task.state === 'failed' ? (
            <Button
              size="sm"
              variant={error.retryWorthwhile ? 'default' : 'outline'}
              onClick={onRetryFailed}
              disabled={retryPending}
            >
              {retryPending ? 'Queueing' : 'Retry failed tasks'}
            </Button>
          ) : null}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

export default FailedTaskDrawer
