import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { ExportDialog } from '@/components/exports/ExportDialog'
import type { UnderlyingOption } from '@/components/exports/ExportDialog'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api, apiUrl } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap } from '@/lib/api/types'
import { formatBytes, formatDateTime, formatInteger, humaniseCode, pluralise } from '@/lib/format'

// Exports: build one, then find it again.
//
// The history half is the half that matters. An export is the moment this app stops being the
// only thing that can read the data, so a finished export whose file cannot be found again is
// the same as no export at all. Every row therefore says exactly one of four things: it is still
// running, it is a file you can have right now, it is a directory that lives at a path, or it
// failed and here is the backend's own sentence about why.

const PAGE_SIZE = 50

/** While anything is queued or running. The export_ready frame arrives on the event stream, but
 *  a failed export emits no such frame, so a slow poll is what keeps a failure from sitting on
 *  screen as "running" forever. */
const ACTIVE_POLL_MS = 3_000

export interface ExportScopeRecord {
  underlying_id?: number | null
  expiry_from?: string | null
  expiry_to?: string | null
  /** Stored resolved, as res_ids, because that is what the export ran with. */
  resolutions?: number[]
  kind?: string | null
  option_type?: string | null
  contract_ids?: number[]
  include_catalog?: boolean
  denormalise?: boolean
}

export interface ExportRow {
  export_id: string
  job_id: string | null
  status: string
  format: string
  layout: string
  compression: string | null
  row_count: number | null
  byte_size: number | null
  sha256: string | null
  created_at: string
  finished_at: string | null
  error_message: string | null
  scope: ExportScopeRecord
}

export interface ExportPage {
  items: ExportRow[]
  next_cursor: string | null
}

export type DeliveryKind = 'working' | 'file' | 'directory' | 'failed' | 'gone'

export interface Delivery {
  kind: DeliveryKind
  label: string
  /** Why there is no download button, when there is not one. */
  reason: string | null
}

/**
 * What can be done with one export row, and why not when the answer is nothing.
 *
 * A partitioned export is the case worth naming: it finished perfectly and there is still no
 * file to attach, because it is a directory of parts and a manifest. The backend answers 409 for
 * that request, so offering the button and letting the user find out is a worse version of
 * saying so here.
 */
export function deliveryFor(row: ExportRow): Delivery {
  if (row.status === 'queued' || row.status === 'running') {
    return { kind: 'working', label: humaniseCode(row.status), reason: null }
  }
  if (row.status === 'failed') {
    return {
      kind: 'failed',
      label: 'Failed',
      reason: row.error_message ?? 'The backend recorded no reason for this failure.',
    }
  }
  if (row.status === 'deleted') {
    return { kind: 'gone', label: 'Deleted', reason: 'The file was removed.' }
  }
  if (row.layout === 'hive') {
    return {
      kind: 'directory',
      label: 'Ready',
      reason:
        'A partitioned export is a directory of parts next to its manifest, so there is no ' +
        'single file to download. Copy it from the exports folder in the data directory.',
    }
  }
  return { kind: 'file', label: 'Ready', reason: null }
}

/** One line describing what an export holds, from the scope it was stored with. Resolutions are
 *  stored as res_ids rather than codes, so they are counted rather than named: printing an id
 *  where the rest of the app prints a Fyers code would read as a different resolution. */
export function describeExportScope(
  scope: ExportScopeRecord,
  underlyingName: string | null,
): string {
  const parts: string[] = []
  parts.push(underlyingName ?? (scope.underlying_id ? 'Underlying ' + String(scope.underlying_id) : 'Every underlying'))

  const kind = scope.kind
  if (!kind || kind === 'BOTH') {
    parts.push('futures and options')
  } else if (kind === 'FUT') {
    parts.push('futures')
  } else {
    parts.push(scope.option_type ? 'options, ' + scope.option_type + ' only' : 'options')
  }

  if (scope.expiry_from || scope.expiry_to) {
    parts.push('expiries ' + (scope.expiry_from ?? 'the first') + ' to ' + (scope.expiry_to ?? 'the last'))
  } else {
    parts.push('every expiry')
  }

  const resolutions = scope.resolutions ?? []
  parts.push(resolutions.length === 0 ? 'every resolution held' : pluralise(resolutions.length, 'resolution'))

  if (scope.contract_ids && scope.contract_ids.length > 0) {
    parts.push(pluralise(scope.contract_ids.length, 'named contract'))
  }
  if (scope.include_catalog) {
    parts.push('with the catalog tables')
  }
  return parts.join(', ')
}

const STATUS_VARIANT: Record<DeliveryKind, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  working: 'outline',
  file: 'secondary',
  directory: 'secondary',
  failed: 'destructive',
  gone: 'outline',
}

export function ExportsRoute() {
  const client = useQueryClient()
  const [building, setBuilding] = useState(false)

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    staleTime: 60_000,
  })

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({ active_only: true }),
    queryFn: () =>
      api.get<UnderlyingOption[]>('/underlyings', { query: { active_only: true } }),
    retry: false,
  })

  const exports = useInfiniteQuery({
    queryKey: queryKeys.exports.list({ limit: PAGE_SIZE }),
    queryFn: ({ pageParam }) =>
      api.get<ExportPage>('/exports', {
        query: { limit: PAGE_SIZE, cursor: pageParam ?? undefined },
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    retry: false,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? []
      const busy = pages.some((page) =>
        page.items.some((row) => row.status === 'queued' || row.status === 'running'),
      )
      return busy ? ACTIVE_POLL_MS : false
    },
  })

  const rows = useMemo(
    () => (exports.data?.pages ?? []).flatMap((page) => page.items),
    [exports.data],
  )

  const nameById = useMemo(() => {
    const byId = new Map<number, string>()
    for (const item of underlyings.data ?? []) {
      byId.set(item.underlying_id, item.display_name)
    }
    return byId
  }, [underlyings.data])

  const remove = useMutation({
    mutationFn: (exportId: string) => api.delete<void>('/exports/' + exportId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.exports.all() })
      toast.success('Export deleted. The file on disk went with it.')
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error) ?? 'That export could not be deleted.')
    },
  })

  if (exports.isError) {
    return (
      <div className="flex min-h-full flex-col">
        <PageHeader title="Exports" />
        <div className="px-5 py-4">
          <EmptyState
            title={
              isRouteMissing(exports.error)
                ? 'Exports are not wired up yet'
                : 'Cannot read the export history'
            }
            description={
              isRouteMissing(exports.error)
                ? 'This build does not serve the exports route yet. The candle store is unaffected.'
                : (apiErrorMessage(exports.error) ?? 'The backend returned an unexpected response.')
            }
            action={
              <Button size="sm" variant="outline" onClick={() => void exports.refetch()}>
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
        title="Exports"
        description="Take the candles out as Parquet or CSV. Reading the store costs no Fyers requests, so an export can run at any time of day."
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => void exports.refetch()}>
              Refresh
            </Button>
            <Button
              size="sm"
              onClick={() => setBuilding(true)}
              disabled={(underlyings.data ?? []).length === 0}
            >
              New export
            </Button>
          </>
        }
      />

      <div className="flex flex-col gap-4 px-5 py-4">
        {bootstrap.data ? (
          <p className="max-w-prose text-xs text-muted-foreground">
            Files are written under the exports folder of {bootstrap.data.data_dir}. Nothing is
            ever written outside that directory.
          </p>
        ) : null}

        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-8 text-xs font-medium">Started</TableHead>
                <TableHead className="h-8 text-xs font-medium">Contents</TableHead>
                <TableHead className="h-8 text-xs font-medium">Shape</TableHead>
                <TableHead className="h-8 text-right text-xs font-medium">Rows</TableHead>
                <TableHead className="h-8 text-right text-xs font-medium">Size</TableHead>
                <TableHead className="h-8 text-xs font-medium">State</TableHead>
                <TableHead className="h-8 text-right text-xs font-medium">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const delivery = deliveryFor(row)
                return (
                  <TableRow key={row.export_id}>
                    <TableCell className="py-2 align-top text-sm">
                      <div className="flex flex-col gap-0.5">
                        <span>{formatDateTime(row.created_at)}</span>
                        {row.finished_at ? (
                          <span className="text-xs text-muted-foreground">
                            finished {formatDateTime(row.finished_at)}
                          </span>
                        ) : null}
                        {row.job_id ? (
                          <Link
                            className="text-xs underline underline-offset-4"
                            to={'/jobs/' + row.job_id}
                          >
                            Job
                          </Link>
                        ) : null}
                      </div>
                    </TableCell>

                    <TableCell className="py-2 align-top text-sm">
                      <span className="max-w-prose">
                        {describeExportScope(
                          row.scope ?? {},
                          row.scope?.underlying_id
                            ? (nameById.get(row.scope.underlying_id) ?? null)
                            : null,
                        )}
                      </span>
                    </TableCell>

                    <TableCell className="py-2 align-top text-sm">
                      <div className="flex flex-col gap-0.5">
                        <span>
                          {row.format}
                          {row.layout === 'hive' ? ', partitioned' : ''}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {row.compression ?? 'no compression'}
                        </span>
                      </div>
                    </TableCell>

                    <TableCell className="py-2 text-right align-top text-sm tabular-nums">
                      {formatInteger(row.row_count)}
                    </TableCell>

                    <TableCell className="py-2 text-right align-top text-sm tabular-nums">
                      {formatBytes(row.byte_size)}
                    </TableCell>

                    <TableCell className="py-2 align-top">
                      <div className="flex flex-col gap-1">
                        <Badge variant={STATUS_VARIANT[delivery.kind]}>{delivery.label}</Badge>
                        {delivery.reason ? (
                          <span
                            className={
                              delivery.kind === 'failed'
                                ? 'max-w-prose text-xs text-destructive'
                                : 'max-w-prose text-xs text-muted-foreground'
                            }
                          >
                            {delivery.reason}
                          </span>
                        ) : null}
                        {row.sha256 ? (
                          <span
                            className="font-mono text-[0.65rem] text-muted-foreground"
                            title={row.sha256}
                          >
                            sha256 {row.sha256.slice(0, 12)}
                          </span>
                        ) : null}
                      </div>
                    </TableCell>

                    <TableCell className="py-2 align-top">
                      <div className="flex items-center justify-end gap-1.5">
                        {delivery.kind === 'file' ? (
                          <Button size="sm" variant="outline" asChild>
                            <a
                              href={apiUrl('/exports/' + row.export_id + '/file')}
                              download
                            >
                              Download
                            </a>
                          </Button>
                        ) : null}
                        {row.status === 'deleted' ? null : (
                          <AlertDialog>
                            <AlertDialogTrigger asChild>
                              <Button size="sm" variant="ghost">
                                Delete
                              </Button>
                            </AlertDialogTrigger>
                            <AlertDialogContent>
                              <AlertDialogHeader>
                                <AlertDialogTitle>Delete this export?</AlertDialogTitle>
                                <AlertDialogDescription>
                                  The file is removed from disk and the row goes with it. The
                                  candles it was made from are untouched, so the same export can
                                  be built again at any time.
                                </AlertDialogDescription>
                              </AlertDialogHeader>
                              <AlertDialogFooter>
                                <AlertDialogCancel>Keep it</AlertDialogCancel>
                                <AlertDialogAction onClick={() => remove.mutate(row.export_id)}>
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

          {exports.isPending ? (
            <p className="px-3 py-4 text-sm text-muted-foreground">Reading the export history.</p>
          ) : null}

          {!exports.isPending && rows.length === 0 ? (
            <EmptyState
              className="rounded-none border-0"
              title="No exports yet"
              description={
                (underlyings.data ?? []).length === 0
                  ? 'There are no underlyings registered, so there is nothing to export. Add one on the Underlyings screen and download an expiry first.'
                  : 'An export reads the candle store and writes a Parquet or CSV file into the data directory. It spends no Fyers requests.'
              }
              action={
                (underlyings.data ?? []).length === 0 ? null : (
                  <Button size="sm" onClick={() => setBuilding(true)}>
                    New export
                  </Button>
                )
              }
            />
          ) : null}
        </div>

        {exports.hasNextPage ? (
          <div>
            <Button
              size="sm"
              variant="outline"
              disabled={exports.isFetchingNextPage}
              onClick={() => void exports.fetchNextPage()}
            >
              {exports.isFetchingNextPage ? 'Loading' : 'Load older exports'}
            </Button>
          </div>
        ) : null}
      </div>

      <ExportDialog
        open={building}
        onOpenChange={setBuilding}
        underlyings={underlyings.data ?? []}
      />
    </div>
  )
}

export default ExportsRoute
