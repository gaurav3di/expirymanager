import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'

import type { DataTableProps } from '@/components/common/DataTable'
import { DataTable } from '@/components/common/DataTable'
import { PageHeader } from '@/components/common/PageHeader'
import type { JobRow } from '@/components/jobs/JobProgressHeader'
import {
  JOB_KINDS,
  JOB_STATUSES,
  JobStateBadge,
  anyJobLive,
  deriveJobState,
  jobProgress,
} from '@/components/jobs/JobProgressHeader'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api/client'
import type { QueryParams } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, Paged } from '@/lib/api/types'
import { EMPTY_VALUE, formatDuration, formatInteger, formatRelative, humaniseCode } from '@/lib/format'

// The job list.
//
// A backfill runs for hours, so this is the screen the user leaves open. Two things follow from
// that and both are visible in the query below.
//
// REST IS AUTHORITATIVE. The SSE stream patches these very rows in the cache by key, which is
// why the key comes from the factory and the body is stored in the documented {items, next_cursor}
// shape. But the screen never depends on the stream: it refetches on its own whenever any job on
// the page can still move. A stream that drops degrades the refresh rate from a quarter of a
// second to five seconds and changes nothing else. A screen that stopped polling once the stream
// connected would freeze on the last frame it happened to receive, showing a stale number as
// current, which is the one failure mode this architecture forbids.
//
// A PAGE OF ONLY TERMINAL JOBS IS NOT POLLED. Nothing on it can change, and a timer that wakes
// every five seconds to re-read a month of finished jobs is a cost with no reader.

const PAGE_LIMIT = 25

/** Faster than the catalogue because a running job changes constantly; slower than the stream,
 *  which is the point. */
const LIVE_POLL_MS = 5_000
const IDLE_POLL_MS = 60_000

const ALL = 'all'

function MiniProgress({ job }: { job: JobRow }) {
  const progress = jobProgress(job)
  return (
    <div className="flex min-w-36 flex-col gap-1">
      <Progress value={progress.fraction * 100} />
      <span className="text-[0.7rem] tabular-nums text-muted-foreground">
        {formatInteger(progress.settled)} of {formatInteger(progress.total)}, {progress.percentLabel}
      </span>
    </div>
  )
}

const COLUMNS: DataTableProps<JobRow>['columns'] = [
  {
    id: 'kind',
    header: 'Job',
    accessorFn: (row) => row.kind,
    cell: ({ row }) => (
      <div className="min-w-0">
        <p className="truncate text-sm">{humaniseCode(row.original.kind)}</p>
        <p className="truncate font-mono text-[0.7rem] text-muted-foreground">
          {row.original.job_id}
        </p>
      </div>
    ),
  },
  {
    id: 'state',
    header: 'State',
    accessorFn: (row) => row.status,
    cell: ({ row }) => <JobStateBadge job={row.original} />,
    size: 150,
  },
  {
    id: 'progress',
    header: 'Progress',
    accessorFn: (row) => jobProgress(row).fraction,
    cell: ({ row }) => <MiniProgress job={row.original} />,
    size: 180,
  },
  {
    id: 'failed',
    header: 'Failed',
    accessorFn: (row) => row.failed_tasks,
    cell: ({ row }) => (
      <span className={row.original.failed_tasks > 0 ? 'text-destructive' : undefined}>
        {formatInteger(row.original.failed_tasks)}
      </span>
    ),
    meta: { numeric: true },
    size: 80,
  },
  {
    id: 'empty',
    header: 'Empty',
    accessorFn: (row) => row.empty_tasks,
    cell: ({ row }) => (
      <span className="text-muted-foreground">{formatInteger(row.original.empty_tasks)}</span>
    ),
    meta: { numeric: true },
    size: 80,
  },
  {
    id: 'requests',
    header: 'Requests',
    accessorFn: (row) => row.requests_used,
    cell: ({ row }) => formatInteger(row.original.requests_used),
    meta: { numeric: true },
    size: 100,
  },
  {
    id: 'rows',
    header: 'Rows',
    accessorFn: (row) => row.rows_written,
    cell: ({ row }) => formatInteger(row.original.rows_written),
    meta: { numeric: true },
    size: 110,
  },
  {
    id: 'eta',
    header: 'Left',
    accessorFn: (row) => row.eta_seconds ?? 0,
    cell: ({ row }) => {
      const job = row.original
      if (!deriveJobState(job).isLive || job.eta_seconds === null) {
        return <span className="text-muted-foreground">{EMPTY_VALUE}</span>
      }
      return formatDuration(job.eta_seconds)
    },
    meta: { numeric: true },
    size: 90,
  },
  {
    id: 'created',
    header: 'Created',
    accessorFn: (row) => row.created_at ?? '',
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {formatRelative(row.original.created_at)}
      </span>
    ),
    size: 120,
  },
]

export default function JobsRoute() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<string>(ALL)
  const [kind, setKind] = useState<string>(ALL)
  // One entry per page visited, oldest first. Index 0 is null, which is the first page.
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const [pageIndex, setPageIndex] = useState(0)

  const cursor = cursors[pageIndex] ?? null

  const params: QueryParams = useMemo(
    () => ({
      limit: PAGE_LIMIT,
      ...(status === ALL ? {} : { status }),
      ...(kind === ALL ? {} : { kind }),
      ...(cursor ? { cursor } : {}),
    }),
    [status, kind, cursor],
  )

  const jobs = useQuery({
    queryKey: queryKeys.jobs.list(params),
    queryFn: () => api.get<Paged<JobRow>>('/jobs', { query: params }),
    // The guarantee. Read off the data the query itself holds, so it re-evaluates on every
    // refetch: a page whose last running job just finished stops polling by itself.
    refetchInterval: (query) =>
      anyJobLive(query.state.data?.items ?? []) ? LIVE_POLL_MS : IDLE_POLL_MS,
    // Paging must not blank the table between pages, which on a five second poll would read as
    // a flicker rather than as a load.
    placeholderData: keepPreviousData,
  })

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    staleTime: 60_000,
  })

  const items = jobs.data?.items ?? []
  const parked = items.filter((job) => deriveJobState(job).needsReauth)
  const brokerConnected = bootstrap.data?.broker_connected ?? false

  function resetPaging() {
    setCursors([null])
    setPageIndex(0)
  }

  return (
    <div className="flex min-w-0 flex-col">
      <PageHeader
        title="Jobs"
        description="Everything the pipeline has been asked to do, with live progress."
        actions={
          <Button size="sm" variant="outline" onClick={() => void jobs.refetch()}>
            Refresh
          </Button>
        }
      >
        <Select
          value={status}
          onValueChange={(value) => {
            setStatus(value)
            resetPaging()
          }}
        >
          <SelectTrigger size="sm" className="w-52">
            <SelectValue placeholder="Any state" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Any state</SelectItem>
            {JOB_STATUSES.map((value) => (
              <SelectItem key={value} value={value}>
                {deriveJobState({ status: value }).label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={kind}
          onValueChange={(value) => {
            setKind(value)
            resetPaging()
          }}
        >
          <SelectTrigger size="sm" className="w-56">
            <SelectValue placeholder="Any kind" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Any kind</SelectItem>
            {JOB_KINDS.map((value) => (
              <SelectItem key={value} value={value}>
                {humaniseCode(value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </PageHeader>

      <div className="flex min-w-0 flex-col gap-3 p-5">
        {parked.length > 0 && !brokerConnected ? (
          <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
            {formatInteger(parked.length)}{' '}
            {parked.length === 1 ? 'job is' : 'jobs are'} waiting for a broker login. Nothing failed
            and nothing needs retrying.{' '}
            <Link to="/settings" className="font-medium underline underline-offset-4">
              Log in to Fyers
            </Link>{' '}
            and they continue from where they stopped.
          </p>
        ) : null}

        <DataTable<JobRow>
          data={items}
          columns={COLUMNS}
          getRowId={(job) => job.job_id}
          // The server orders newest first and there is no sort parameter to defer to, so the
          // header sorts the page in front of the user rather than pretending to sort the set.
          manualSorting={false}
          isLoading={jobs.isPending}
          onRowClick={(job) => void navigate('/jobs/' + job.job_id)}
          emptyTitle={status === ALL && kind === ALL ? 'No jobs yet' : 'Nothing matches this filter'}
          emptyDescription={
            status === ALL && kind === ALL
              ? 'A job appears here as soon as you start a download or a discovery.'
              : 'No job has been created with this combination yet.'
          }
          emptyAction={
            status === ALL && kind === ALL ? (
              <Button size="sm" asChild>
                <Link to="/expiries">Choose expiries to download</Link>
              </Button>
            ) : (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setStatus(ALL)
                  setKind(ALL)
                  resetPaging()
                }}
              >
                Clear the filter
              </Button>
            )
          }
          footer={
            <>
              <span>
                Page {formatInteger(pageIndex + 1)}, {formatInteger(items.length)} shown
                {jobs.isFetching ? ', refreshing' : ''}
              </span>
              <span className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={pageIndex === 0}
                  onClick={() => setPageIndex((index) => Math.max(0, index - 1))}
                >
                  Previous
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!jobs.data?.next_cursor}
                  onClick={() => {
                    const next = jobs.data?.next_cursor
                    if (!next) {
                      return
                    }
                    setCursors((previous) => {
                      const trimmed = previous.slice(0, pageIndex + 1)
                      return [...trimmed, next]
                    })
                    setPageIndex((index) => index + 1)
                  }}
                >
                  Next
                </Button>
              </span>
            </>
          }
        />
      </div>
    </div>
  )
}
