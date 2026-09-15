import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { EmptyState } from '@/components/common/EmptyState'
import type { JobDetailRow } from '@/components/jobs/JobProgressHeader'
import { JobProgressHeader, deriveJobState, jobCapabilities, pollIntervalFor } from '@/components/jobs/JobProgressHeader'
import { FailedTaskDrawer } from '@/components/jobs/FailedTaskDrawer'
import type { JobTaskRow } from '@/components/jobs/TaskTable'
import { TASK_TABS, TaskTable, tabByKey, tabCount } from '@/components/jobs/TaskTable'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ApiError, api } from '@/lib/api/client'
import type { QueryParams } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, Paged } from '@/lib/api/types'
import { formatInteger, humaniseCode } from '@/lib/format'

// The job detail.
//
// Live progress at the top, the tasks below it, and a drill-through into any single one of them.
//
// THE REFETCH IS THE SAFETY NET, NOT THE SPEED. The SSE stream patches this exact cache entry,
// so the counters move a quarter of a second after the worker settles a task. The query below
// still refetches on its own cadence for as long as the job can move, so a dropped stream shows
// up as a slower screen and never as a frozen one. pollIntervalFor is read from the job in the
// cache on every refetch, so a job that reaches a terminal state turns its own timer off.
//
// THE ACTIONS ARE THE ONES THE SERVICE WILL ACCEPT. can_pause and friends are recomputed here
// from the status rather than read from the fields the REST body carries, because the stream
// patches status without them: a job the stream has just parked would otherwise keep offering
// Pause. Cancel asks first, because in-flight tasks finish and the answer is not reversible.

const TASK_PAGE_LIMIT = 100

function failureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  return 'The request did not complete.'
}

export default function JobDetailRoute() {
  const params = useParams<{ jobId: string }>()
  const jobId = params.jobId ?? ''
  const client = useQueryClient()
  const navigate = useNavigate()

  const [tabKey, setTabKey] = useState<string>('all')
  const [taskCursors, setTaskCursors] = useState<Array<string | null>>([null])
  const [taskPage, setTaskPage] = useState(0)
  const [selected, setSelected] = useState<JobTaskRow | null>(null)
  const [confirmCancel, setConfirmCancel] = useState(false)

  const job = useQuery({
    queryKey: queryKeys.jobs.detail(jobId),
    queryFn: () => api.get<JobDetailRow>('/jobs/' + jobId),
    enabled: jobId !== '',
    refetchInterval: (query) => {
      const current = query.state.data
      return current ? pollIntervalFor(current) : 10_000
    },
  })

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    staleTime: 60_000,
  })

  const tab = tabByKey(tabKey)
  const taskCursor = taskCursors[taskPage] ?? null

  const taskParams: QueryParams = useMemo(
    () => ({
      limit: TASK_PAGE_LIMIT,
      ...(tab.state === null ? {} : { state: tab.state }),
      ...(taskCursor ? { cursor: taskCursor } : {}),
    }),
    [tab.state, taskCursor],
  )

  const tasks = useQuery({
    queryKey: queryKeys.jobs.tasks(jobId, taskParams),
    queryFn: () => api.get<Paged<JobTaskRow>>('/jobs/' + jobId + '/tasks', { query: taskParams }),
    enabled: jobId !== '',
    // The task_completed frame invalidates this key, so the stream drives the fast path. This
    // interval is what keeps the rows moving when the stream is not there.
    refetchInterval: () => {
      const current = job.data
      return current ? pollIntervalFor(current) : false
    },
    placeholderData: keepPreviousData,
  })

  function invalidateJob() {
    void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
  }

  const pause = useMutation({
    mutationFn: () => api.post<unknown>('/jobs/' + jobId + '/pause'),
    onSuccess: () => {
      invalidateJob()
      toast.success('Job paused')
    },
    onError: (error) => toast.error(failureMessage(error)),
  })

  const resume = useMutation({
    mutationFn: () => api.post<unknown>('/jobs/' + jobId + '/resume'),
    onSuccess: () => {
      invalidateJob()
      toast.success('Job resumed')
    },
    onError: (error) => toast.error(failureMessage(error)),
  })

  const cancel = useMutation({
    mutationFn: () => api.post<{ tasks_cancelled: number }>('/jobs/' + jobId + '/cancel'),
    onSuccess: (result) => {
      invalidateJob()
      toast.success(
        'Cancelled. ' +
          formatInteger(result.tasks_cancelled) +
          ' pending tasks dropped. Tasks already in flight finish and write their data.',
      )
    },
    onError: (error) => toast.error(failureMessage(error)),
  })

  const retryFailed = useMutation({
    mutationFn: () =>
      api.post<{ job_id: string; parent_job_id: string; total_tasks: number }>(
        '/jobs/' + jobId + '/retry-failed',
      ),
    onSuccess: (result) => {
      invalidateJob()
      setSelected(null)
      toast.success(
        'Queued ' + formatInteger(result.total_tasks) + ' failed tasks as a new job.',
        {
          action: {
            label: 'Open it',
            onClick: () => void navigate('/jobs/' + result.job_id),
          },
        },
      )
    },
    onError: (error) => toast.error(failureMessage(error)),
  })

  if (job.isPending) {
    return <p className="p-5 text-sm text-muted-foreground">Reading the job.</p>
  }

  if (job.isError || !job.data) {
    const error = job.error instanceof ApiError ? job.error : null
    return (
      <div className="p-5">
        <EmptyState
          title={error?.status === 404 ? 'No such job' : 'Cannot read this job'}
          description={
            error?.status === 404
              ? 'This job id is not in the job table. It may have been removed by a maintenance run.'
              : failureMessage(job.error)
          }
          action={
            <Button size="sm" variant="outline" asChild>
              <Link to="/jobs">Back to the job list</Link>
            </Button>
          }
        />
      </div>
    )
  }

  const detail = job.data
  const state = deriveJobState(detail)
  const can = jobCapabilities(detail)
  const brokerConnected = bootstrap.data?.broker_connected ?? false
  const taskStates = detail.task_states
  const rows = tasks.data?.items ?? []

  function changeTab(next: string) {
    setTabKey(next)
    setTaskCursors([null])
    setTaskPage(0)
  }

  return (
    <div className="flex min-w-0 flex-col">
      <JobProgressHeader
        job={detail}
        brokerConnected={brokerConnected}
        actions={
          <>
            <Button size="sm" variant="outline" asChild>
              <Link to="/jobs">All jobs</Link>
            </Button>
            {can.canPause ? (
              <Button
                size="sm"
                variant="outline"
                disabled={pause.isPending}
                onClick={() => pause.mutate()}
              >
                Pause
              </Button>
            ) : null}
            {can.canResume ? (
              <Button
                size="sm"
                variant={state.needsReauth && !brokerConnected ? 'outline' : 'default'}
                disabled={resume.isPending}
                onClick={() => resume.mutate()}
                // A parked job can be resumed at any time, but with no token the auth gate parks
                // it again immediately. Saying so is better than hiding the button or letting it
                // look like it did nothing.
                title={
                  state.needsReauth && !brokerConnected
                    ? 'The broker token is still missing, so this job will park again until you log in.'
                    : undefined
                }
              >
                Resume
              </Button>
            ) : null}
            {can.canRetryFailed ? (
              <Button
                size="sm"
                variant="outline"
                disabled={retryFailed.isPending}
                onClick={() => retryFailed.mutate()}
              >
                Retry {formatInteger(detail.failed_tasks)} failed
              </Button>
            ) : null}
            {can.canCancel ? (
              <Button
                size="sm"
                variant="destructive"
                disabled={cancel.isPending}
                onClick={() => setConfirmCancel(true)}
              >
                Cancel
              </Button>
            ) : null}
          </>
        }
      />

      <div className="flex min-w-0 flex-col gap-3 p-5">
        {detail.error_text ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm">
            <span className="text-muted-foreground">Job error: </span>
            {detail.error_text}
          </p>
        ) : null}

        {detail.parent_job_id || detail.child_job_ids.length > 0 ? (
          <p className="text-xs text-muted-foreground">
            {detail.parent_job_id ? (
              <>
                Retry of{' '}
                <Link
                  to={'/jobs/' + detail.parent_job_id}
                  className="font-mono underline underline-offset-4"
                >
                  {detail.parent_job_id}
                </Link>
                .{' '}
              </>
            ) : null}
            {detail.child_job_ids.length > 0 ? (
              <>
                Retried by{' '}
                {detail.child_job_ids.map((childId, index) => (
                  <span key={childId}>
                    {index > 0 ? ', ' : ''}
                    <Link
                      to={'/jobs/' + childId}
                      className="font-mono underline underline-offset-4"
                    >
                      {childId}
                    </Link>
                  </span>
                ))}
                .
              </>
            ) : null}
          </p>
        ) : null}

        <Tabs value={tabKey} onValueChange={changeTab}>
          <TabsList>
            {TASK_TABS.map((entry) => (
              <TabsTrigger key={entry.key} value={entry.key}>
                {entry.label}
                <span className="ml-1.5 tabular-nums text-muted-foreground">
                  {formatInteger(tabCount(taskStates, entry))}
                </span>
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value={tabKey} className="mt-3">
            <TaskTable
              tasks={rows}
              isLoading={tasks.isPending}
              onSelect={setSelected}
              emptyTitle={
                tab.key === 'failed'
                  ? 'Nothing failed'
                  : tab.key === 'empty'
                    ? 'Nothing came back empty'
                    : tab.key === 'skipped'
                      ? 'Nothing was skipped'
                      : 'No tasks'
              }
              emptyDescription={
                tab.key === 'empty'
                  ? 'An empty task is a request Fyers answered with no_data. None of this job did.'
                  : tab.key === 'skipped'
                    ? 'A skipped task was never requested because the window was already covered or sealed.'
                    : undefined
              }
              footer={
                <>
                  <span>
                    {formatInteger(rows.length)} of {formatInteger(tabCount(taskStates, tab))}{' '}
                    {humaniseCode(tab.label).toLowerCase()} tasks
                    {tasks.isFetching ? ', refreshing' : ''}
                  </span>
                  <span className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={taskPage === 0}
                      onClick={() => setTaskPage((index) => Math.max(0, index - 1))}
                    >
                      Previous
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!tasks.data?.next_cursor}
                      onClick={() => {
                        const next = tasks.data?.next_cursor
                        if (!next) {
                          return
                        }
                        setTaskCursors((previous) => [
                          ...previous.slice(0, taskPage + 1),
                          next,
                        ])
                        setTaskPage((index) => index + 1)
                      }}
                    >
                      Next
                    </Button>
                  </span>
                </>
              }
            />
          </TabsContent>
        </Tabs>
      </div>

      <FailedTaskDrawer
        task={selected}
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSelected(null)
          }
        }}
        onRetryFailed={can.canRetryFailed ? () => retryFailed.mutate() : undefined}
        retryPending={retryFailed.isPending}
      />

      <AlertDialog open={confirmCancel} onOpenChange={setConfirmCancel}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cancel this job</AlertDialogTitle>
            <AlertDialogDescription>
              Pending tasks are dropped. Tasks already in flight finish and write their candles, so
              the partial result stays valid and the coverage ledger keeps describing exactly what
              was fetched. A cancelled job cannot be resumed.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep running</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmCancel(false)
                cancel.mutate()
              }}
            >
              Cancel the job
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
