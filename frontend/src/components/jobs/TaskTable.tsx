import type { ReactNode } from 'react'
import { cn } from 'cn'

import type { DataTableProps } from '@/components/common/DataTable'
import { DataTable } from '@/components/common/DataTable'
import { Badge } from '@/components/ui/badge'
import {
  EMPTY_VALUE,
  formatDateTime,
  formatInteger,
  formatLatency,
  humaniseCode,
} from '@/lib/format'

// The task breakdown, and the vocabulary it is read with.
//
// The one distinction this table exists to make: EMPTY IS NOT FAILED. A task in `empty` asked
// Fyers for a window and Fyers answered "no_data", which is a successful request and a true
// answer about a contract that did not trade. Rendering it beside `failed` in red would send the
// user hunting for a fault in the six thousand windows of a backfill that are simply quiet. It
// gets its own tab, its own tone and its own sentence.

/** PIPELINE.md section 9.2. The seven states the task table can hold. */
export const TASK_STATES = [
  'pending',
  'leased',
  'done',
  'empty',
  'failed',
  'skipped',
  'cancelled',
] as const

export type TaskStateName = (typeof TASK_STATES)[number]

/**
 * One row of `GET /api/v1/jobs/{job_id}/tasks`.
 *
 * Declared here rather than imported from lib/api/types.ts, whose `JobTask` was written against
 * the API.md sketch: it types `task_id` as a string (the column is an integer), carries `res_id`
 * where the served row carries the Fyers `resolution` code, names task states this pipeline does
 * not have (`ok`, `running`, `blocked_auth`) and omits the ones it does (`leased`, `cancelled`),
 * and has no seq, expiry_date, max_attempts or response_bytes. This mirrors the served TaskRow.
 */
export interface JobTaskRow {
  task_id: number
  job_id: string
  seq: number
  kind: string
  state: string
  priority: number

  underlying_id: number | null
  contract_id: number | null
  fyers_symbol: string | null
  expiry_date: string | null
  resolution: string | null
  range_from: string | null
  range_to: string | null
  include_oi: boolean

  attempt: number
  max_attempts: number
  not_before: string | null
  parent_task_id: number | null

  http_status: number | null
  error_code: string | null
  error_message: string | null
  latency_ms: number | null
  response_bytes: number | null
  row_count: number | null
  first_ts: string | null
  last_ts: string | null

  /** The stored path never crosses the wire. Only whether one exists. */
  has_raw_body: boolean
  request_params_json: unknown

  started_at: string | null
  finished_at: string | null
  created_at: string | null
  updated_at: string | null
}

export type TaskTone = 'neutral' | 'running' | 'quiet' | 'good' | 'bad'

export interface TaskStateReading {
  label: string
  tone: TaskTone
  /** One sentence the drawer prints verbatim. */
  meaning: string
  /** True only for a state that actually wants a human. */
  needsAttention: boolean
}

const TASK_READINGS: Record<TaskStateName, TaskStateReading> = {
  pending: {
    label: 'Pending',
    tone: 'neutral',
    meaning: 'Waiting to be leased by a worker. Nothing has been requested yet.',
    needsAttention: false,
  },
  leased: {
    label: 'In flight',
    tone: 'running',
    meaning: 'Held by a worker right now. The lease is reclaimed automatically if the worker dies.',
    needsAttention: false,
  },
  done: {
    label: 'Done',
    tone: 'good',
    meaning: 'The request succeeded and its candles were written.',
    needsAttention: false,
  },
  empty: {
    label: 'Empty',
    tone: 'quiet',
    // The sentence that stops a quiet contract reading as a fault.
    meaning:
      'The request succeeded and Fyers answered no_data for this window. That is a true answer ' +
      'about a contract that did not trade, not a failure, and retrying it would spend a request ' +
      'to be told the same thing.',
    needsAttention: false,
  },
  failed: {
    label: 'Failed',
    tone: 'bad',
    meaning:
      'Either a fatal refusal from the vendor, or a transient error that used up every attempt. ' +
      'This window holds no data and Retry failed tasks is what fills it.',
    needsAttention: true,
  },
  skipped: {
    label: 'Skipped',
    tone: 'quiet',
    meaning:
      'Never requested. The window was already covered, the contract was sealed, or it sits ' +
      'before a no_data boundary the planner had already established.',
    needsAttention: false,
  },
  cancelled: {
    label: 'Cancelled',
    tone: 'neutral',
    meaning: 'Dropped when the job was cancelled, before any request was made.',
    needsAttention: false,
  },
}

function isKnownTaskState(state: string): state is TaskStateName {
  return Object.prototype.hasOwnProperty.call(TASK_READINGS, state)
}

/** How one task state reads. An unknown state is named, never folded into a neighbour. */
export function describeTaskState(state: string): TaskStateReading {
  if (!isKnownTaskState(state)) {
    return {
      label: humaniseCode(state),
      tone: 'neutral',
      meaning: 'This build does not recognise the task state ' + state + '.',
      needsAttention: false,
    }
  }
  return TASK_READINGS[state]
}

type BadgeVariant = 'default' | 'secondary' | 'outline' | 'destructive' | 'ghost'

const TASK_TONE_VARIANT: Record<TaskTone, BadgeVariant> = {
  neutral: 'outline',
  running: 'default',
  // Ghost, not outline: empty and skipped are true, uninteresting answers, and they are the bulk
  // of the rows in any wide backfill. They must not compete with a failure for attention.
  quiet: 'ghost',
  good: 'secondary',
  bad: 'destructive',
}

export function TaskStateBadge({ state }: { state: string }) {
  const reading = describeTaskState(state)
  return (
    <Badge variant={TASK_TONE_VARIANT[reading.tone]} title={reading.meaning}>
      {reading.label}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

export interface TaskTab {
  key: string
  label: string
  /** The `state` query parameter sent to the tasks route. null means every state. */
  state: TaskStateName | null
  /** The task states counted into this tab's badge. */
  counts: readonly TaskStateName[]
}

/**
 * The four tabs, in the order the deliverable names them.
 *
 * Failed, Empty and Skipped are each their own filter rather than one "not done" bucket,
 * because the three have completely different meanings and only one of them is a problem.
 */
export const TASK_TABS: readonly TaskTab[] = [
  { key: 'all', label: 'All', state: null, counts: TASK_STATES },
  { key: 'failed', label: 'Failed', state: 'failed', counts: ['failed'] },
  { key: 'empty', label: 'Empty', state: 'empty', counts: ['empty'] },
  { key: 'skipped', label: 'Skipped', state: 'skipped', counts: ['skipped'] },
]

export function tabByKey(key: string): TaskTab {
  return TASK_TABS.find((tab) => tab.key === key) ?? TASK_TABS[0]
}

/**
 * The number on a tab, read from the job's own live `task_states` aggregate.
 *
 * Read from the aggregate and not from the rows on screen: the rows are one cursor page of up to
 * five hundred, and a count taken from them would read "37 failed" on a job with nine hundred.
 */
export function tabCount(taskStates: Record<string, number>, tab: TaskTab): number {
  if (tab.state === null) {
    return Object.values(taskStates).reduce((total, value) => total + value, 0)
  }
  return tab.counts.reduce((total, state) => total + (taskStates[state] ?? 0), 0)
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

/** What a task was pointed at. Contract tasks carry a symbol; discovery tasks do not. */
export function taskSubject(task: JobTaskRow): string {
  if (task.fyers_symbol) {
    return task.fyers_symbol
  }
  if (task.expiry_date) {
    return 'Expiry ' + task.expiry_date
  }
  if (task.underlying_id !== null) {
    return 'Underlying ' + String(task.underlying_id)
  }
  return humaniseCode(task.kind)
}

/** The requested window, as stored. Both edges are IST text written by the planner. */
export function taskWindow(task: JobTaskRow): string {
  if (!task.range_from && !task.range_to) {
    return EMPTY_VALUE
  }
  return String(task.range_from ?? EMPTY_VALUE) + ' to ' + String(task.range_to ?? EMPTY_VALUE)
}

/** The code and the attempt count, which together say whether retrying is even plausible. */
export function taskErrorLabel(task: JobTaskRow): string {
  if (!task.error_code && !task.http_status) {
    return ''
  }
  const parts: string[] = []
  if (task.error_code) {
    parts.push(task.error_code)
  }
  if (task.http_status !== null) {
    parts.push('HTTP ' + String(task.http_status))
  }
  return parts.join(' ')
}

const COLUMNS: DataTableProps<JobTaskRow>['columns'] = [
  {
    id: 'seq',
    header: 'Seq',
    accessorFn: (row) => row.seq,
    cell: ({ row }) => formatInteger(row.original.seq),
    meta: { numeric: true },
    size: 64,
  },
  {
    id: 'subject',
    header: 'Subject',
    accessorFn: (row) => taskSubject(row),
    cell: ({ row }) => (
      <span className="font-mono text-xs">{taskSubject(row.original)}</span>
    ),
  },
  {
    id: 'resolution',
    header: 'Res',
    accessorFn: (row) => row.resolution ?? '',
    cell: ({ row }) => row.original.resolution ?? EMPTY_VALUE,
    size: 72,
  },
  {
    id: 'window',
    header: 'Window',
    accessorFn: (row) => row.range_from ?? '',
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {taskWindow(row.original)}
      </span>
    ),
  },
  {
    id: 'state',
    header: 'State',
    accessorFn: (row) => row.state,
    cell: ({ row }) => <TaskStateBadge state={row.original.state} />,
    size: 110,
  },
  {
    id: 'attempt',
    header: 'Try',
    accessorFn: (row) => row.attempt,
    cell: ({ row }) =>
      String(row.original.attempt) + ' of ' + String(row.original.max_attempts),
    meta: { numeric: true },
    size: 80,
  },
  {
    id: 'rows',
    header: 'Rows',
    accessorFn: (row) => row.row_count ?? 0,
    cell: ({ row }) => formatInteger(row.original.row_count),
    meta: { numeric: true },
    size: 90,
  },
  {
    id: 'latency',
    header: 'Latency',
    accessorFn: (row) => row.latency_ms ?? 0,
    cell: ({ row }) => formatLatency(row.original.latency_ms),
    meta: { numeric: true },
    size: 90,
  },
  {
    id: 'error',
    header: 'Error',
    accessorFn: (row) => row.error_code ?? '',
    cell: ({ row }) => {
      const task = row.original
      const label = taskErrorLabel(task)
      if (!label && !task.error_message) {
        return <span className="text-muted-foreground">{EMPTY_VALUE}</span>
      }
      return (
        <div className="min-w-0">
          {label ? (
            <span
              className={cn(
                'font-mono text-xs',
                task.state === 'failed' ? 'text-destructive' : 'text-muted-foreground',
              )}
            >
              {label}
            </span>
          ) : null}
          {task.error_message ? (
            <p className="truncate text-xs text-muted-foreground" title={task.error_message}>
              {task.error_message}
            </p>
          ) : null}
        </div>
      )
    },
  },
  {
    id: 'finished',
    header: 'Updated',
    accessorFn: (row) => row.updated_at ?? '',
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {formatDateTime(row.original.updated_at)}
      </span>
    ),
  },
]

export interface TaskTableProps {
  tasks: JobTaskRow[]
  isLoading?: boolean
  onSelect?: (task: JobTaskRow) => void
  emptyTitle?: string
  emptyDescription?: string
  footer?: ReactNode
}

export function TaskTable({
  tasks,
  isLoading = false,
  onSelect,
  emptyTitle = 'No tasks here',
  emptyDescription,
  footer,
}: TaskTableProps) {
  return (
    <DataTable<JobTaskRow>
      data={tasks}
      columns={COLUMNS}
      getRowId={(task) => String(task.task_id)}
      // The page is held whole, so a header click reorders what is on screen without spending a
      // round trip. The server has no sort parameter on this route to defer to.
      manualSorting={false}
      isLoading={isLoading}
      onRowClick={onSelect}
      emptyTitle={emptyTitle}
      emptyDescription={emptyDescription}
      footer={footer}
      maxHeight="60vh"
    />
  )
}

export default TaskTable
