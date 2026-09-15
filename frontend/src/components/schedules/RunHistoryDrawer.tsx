import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { EmptyState } from '@/components/common/EmptyState'
import { apiErrorMessage } from '@/components/settings/BrokerPanel'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatDateTime, formatRelative } from '@/lib/format'
import { OutcomeBadge, describeCron, describeRun } from '@/routes/schedules'
import type { ScheduleRow, ScheduleRunRow } from '@/routes/schedules'

// Run history for one schedule.
//
// Every fire is here, including the ones that did nothing, because "did nothing" is the most
// common outcome on this app and the user has to be able to tell a skip apart from a silence.
// A schedule that has never fired and a schedule whose fires all skipped look identical on the
// list screen and completely different here.
//
// The note is printed in full for every run, and the segments of it that contradict the outcome
// are repeated at the top of the run in the destructive tone. Maintenance is the reason: it
// reports `completed` while its own note says the trading day refresh FAILED, and that note is
// the only record of it anywhere in the product.

const RUN_LIMIT = 50

export interface RunHistoryDrawerProps {
  /** The schedule to show. null closes the drawer. */
  schedule: ScheduleRow | null
  onOpenChange: (open: boolean) => void
}

export function RunHistoryDrawer({ schedule, onOpenChange }: RunHistoryDrawerProps) {
  const scheduleId = schedule?.schedule_id ?? ''

  const runs = useQuery({
    queryKey: queryKeys.schedules.runs(scheduleId, { limit: RUN_LIMIT }),
    queryFn: () =>
      api.get<ScheduleRunRow[]>('/schedules/' + scheduleId + '/runs', {
        query: { limit: RUN_LIMIT },
      }),
    enabled: scheduleId !== '',
    retry: false,
  })

  const items = runs.data ?? []

  return (
    <Sheet open={schedule !== null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full gap-0 sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{schedule?.name ?? 'Run history'}</SheetTitle>
          <SheetDescription>
            {schedule
              ? describeCron(schedule.cron, schedule.timezone) +
                '. The last ' +
                String(RUN_LIMIT) +
                ' fires, newest first.'
              : null}
          </SheetDescription>
        </SheetHeader>

        {/* Its own scroll region so the sheet header stays put and the styled scrollbar from
            index.css applies instead of a platform one on a dark panel. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          {runs.isPending && scheduleId !== '' ? (
            <p className="text-sm text-muted-foreground">Reading the history.</p>
          ) : null}

          {runs.isError ? (
            <EmptyState
              title="Cannot read the run history"
              description={
                apiErrorMessage(runs.error) ?? 'The backend returned an unexpected response.'
              }
              action={
                <Button size="sm" variant="outline" onClick={() => void runs.refetch()}>
                  Retry
                </Button>
              }
            />
          ) : null}

          {!runs.isPending && !runs.isError && items.length === 0 ? (
            <EmptyState
              title="This schedule has never fired"
              description={
                schedule?.enabled
                  ? 'It is switched on, so the next fire will be its first. Run now fires it immediately without waiting for the timer.'
                  : 'It is switched off, so the timer will not reach it. Switch it on, or use Run now for a single fire.'
              }
            />
          ) : null}

          <ol className="flex flex-col gap-2">
            {items.map((run) => {
              const described = describeRun(run.outcome, run.note, schedule?.kind)
              return (
                <li key={run.run_id} className="rounded-lg border px-3 py-2">
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <OutcomeBadge description={described} />
                      <span className="text-sm">{formatDateTime(run.fired_at)}</span>
                      <span className="text-xs text-muted-foreground">
                        {formatRelative(run.fired_at)}
                      </span>
                    </div>
                    {run.job_id ? (
                      <Link
                        className="text-xs underline underline-offset-4"
                        to={'/jobs/' + run.job_id}
                      >
                        Open the job
                      </Link>
                    ) : null}
                  </div>

                  {described.meaning ? (
                    <p className="mt-1 max-w-prose text-xs text-muted-foreground">
                      {described.meaning}
                    </p>
                  ) : null}

                  {described.findings.length > 0 ? (
                    <ul className="mt-1 flex flex-col gap-0.5 text-xs text-destructive">
                      {described.findings.map((finding) => (
                        <li key={finding}>{finding}</li>
                      ))}
                    </ul>
                  ) : null}

                  {run.note ? (
                    <p className="mt-1 max-w-prose break-words font-mono text-[0.7rem] text-muted-foreground">
                      {run.note}
                    </p>
                  ) : null}
                </li>
              )
            })}
          </ol>
        </div>
      </SheetContent>
    </Sheet>
  )
}

export default RunHistoryDrawer
