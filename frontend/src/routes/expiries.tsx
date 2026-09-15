import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { DownloadSheet } from '@/components/download/DownloadSheet'
import {
  ExpirySelectTable,
  selectedRows,
  type ExpiryPageWire,
  type ExpiryRow,
  type ExpirySelection,
} from '@/components/download/ExpirySelectTable'
import { SelectionBar } from '@/components/download/SelectionBar'
import { describeCatalogFailure, type UnderlyingWire } from '@/components/underlyings/AddUnderlyingDialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatInteger, pluralise } from '@/lib/format'

// The screen the product exists for.
//
// Pick an underlying, see every expiry with how much of it is already held, tick the ones you
// want, and price the download before it is started. Nothing on the way to a job skips the
// price: the sheet cannot be committed without the request count the preview showed.
//
// Two backend facts shape the controls.
//
//  - Expiry DATES and expiry CONTRACTS are discovered by different requests. A date arrives from
//    the expiry dates endpoint, one request per 366 day window; the contract list for that date
//    arrives from the underlying symbols endpoint, one request per expiry. So a row can exist
//    with no contracts, which is why the table says "discovery needed" rather than "no contracts".
//  - The planner refuses to price a sheet on which NO selected expiry has contracts, because
//    there is nothing local to price. The remedy is a contract discovery run, and the only thing
//    in the system that creates one is the contract discovery schedule, so that is what the
//    button below fires. It is named for what it does.

/** Today as an IST calendar date. A calendar fact, not a session rule: nothing here decides
 *  whether a day trades, because observed sessions run on Saturdays and Sundays too. */
const istDateFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Kolkata',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

export function istToday(now: Date = new Date()): string {
  return istDateFormat.format(now)
}

export interface DiscoverRange {
  range_from: string
  range_to: string
}

/** The range an expiry discovery starts from: everything the registry says this underlying has
 *  data for, up to today. The backend clamps the far end to the last day the vendor serves and
 *  says so in the response, so nothing here has to guess where that is. */
export function discoverDefaults(
  underlying: Pick<UnderlyingWire, 'data_from'>,
  today: string = istToday(),
): DiscoverRange {
  return { range_from: underlying.data_from, range_to: today }
}

export interface DiscoverAcceptedWire {
  job_id: string
  kind: string
  status: string
  total_tasks: number
  est_requests: number
  range_from: string
  range_to: string
  clamped: boolean
  windows: Array<{ range_from: string; range_to: string }>
}

/** What the 202 means, in one sentence. The clamp is called out because the range the user typed
 *  and the range that will be asked for are not always the same. */
export function describeDiscoverAccepted(accepted: DiscoverAcceptedWire): string {
  return (
    pluralise(accepted.windows.length, 'window', 'windows') +
    ' queued over ' +
    accepted.range_from +
    ' to ' +
    accepted.range_to +
    (accepted.clamped ? ', clamped to the last day the vendor serves' : '') +
    '. One request each.'
  )
}

interface ScheduleRowWire {
  schedule_id: string
  name: string
  kind: string
  enabled: boolean
  is_builtin: boolean
}

interface RunNowWire {
  schedule_id: string
  run_id: string | null
  job_id: string | null
  outcome: string
  note: string | null
}

/** The schedule that creates contract discovery jobs, found by kind rather than by a hardcoded
 *  id, preferring the seeded one. Null when the installation has none, in which case the button
 *  is not offered and the sheet says so instead of failing on click. */
export function contractDiscoverySchedule(
  schedules: readonly ScheduleRowWire[] | undefined,
): ScheduleRowWire | null {
  const matching = (schedules ?? []).filter((row) => row.kind === 'contract_discovery')
  return matching.find((row) => row.is_builtin) ?? matching[0] ?? null
}

/** The selection, and which underlying it belongs to.
 *
 *  Held together rather than separately because an expiry date selected under NIFTY means nothing
 *  under SENSEX, and a plan request carries dates without saying which underlying they came from.
 *  Reading it back through `selectionFor` is what stops one screen's ticks being priced against
 *  another underlying after a navigation that only changed the query string. */
export interface OwnedSelection {
  underlyingId: number | null
  selection: ExpirySelection
}

export function selectionFor(owned: OwnedSelection, underlyingId: number | null): ExpirySelection {
  return owned.underlyingId !== null && owned.underlyingId === underlyingId
    ? owned.selection
    : {}
}

export default function ExpiriesRoute() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [owned, setOwned] = useState<OwnedSelection>({ underlyingId: null, selection: {} })
  const [sheetOpen, setSheetOpen] = useState(false)
  const [discoverOpen, setDiscoverOpen] = useState(false)
  const [rangeFrom, setRangeFrom] = useState('')
  const [rangeTo, setRangeTo] = useState('')
  const [discoverRange, setDiscoverRange] = useState<DiscoverRange | null>(null)

  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list({}),
    queryFn: () => api.get<UnderlyingWire[]>('/underlyings'),
  })

  const schedules = useQuery({
    queryKey: queryKeys.schedules.list(),
    queryFn: () => api.get<ScheduleRowWire[]>('/schedules'),
  })

  const rows = underlyings.data ?? []
  const requested = Number(searchParams.get('underlying') ?? '')
  const underlying =
    rows.find((row) => row.underlying_id === requested) ?? rows[0] ?? null

  // The address bar is the source of truth for which underlying is open, so a link into this
  // screen and a pick from the selector land in the same place.
  useEffect(() => {
    if (underlying && underlying.underlying_id !== requested) {
      setSearchParams({ underlying: String(underlying.underlying_id) }, { replace: true })
    }
  }, [underlying, requested, setSearchParams])

  // Derived rather than reset in an effect: a selection that belongs to another underlying is
  // simply not this screen's selection, and there is nothing to clean up.
  const selection = selectionFor(owned, underlying?.underlying_id ?? null)
  const setSelection = (next: ExpirySelection) => {
    setOwned({ underlyingId: underlying?.underlying_id ?? null, selection: next })
  }

  const listParams = useMemo(
    () => ({
      ...(rangeFrom ? { from: rangeFrom } : {}),
      ...(rangeTo ? { to: rangeTo } : {}),
    }),
    [rangeFrom, rangeTo],
  )

  const expiries = useInfiniteQuery({
    queryKey: queryKeys.underlyings.expiries(underlying?.underlying_id ?? 0, listParams),
    enabled: underlying !== null,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api.get<ExpiryPageWire>(
        '/underlyings/' + String(underlying?.underlying_id ?? 0) + '/expiries',
        { query: { ...listParams, ...(pageParam ? { cursor: pageParam } : {}) } },
      ),
    getNextPageParam: (last) => last.next_cursor,
  })

  const expiryRows: ExpiryRow[] = useMemo(
    () => (expiries.data?.pages ?? []).flatMap((page) => page.items),
    [expiries.data],
  )
  const picked = useMemo(() => selectedRows(selection), [selection])

  const discover = useMutation({
    mutationFn: (input: { underlyingId: number; range: DiscoverRange }) =>
      api.post<DiscoverAcceptedWire>(
        '/underlyings/' + String(input.underlyingId) + '/expiries/discover',
        { body: input.range },
      ),
    onSuccess: (accepted) => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      toast.success(describeDiscoverAccepted(accepted))
      setDiscoverOpen(false)
      void navigate('/jobs/' + accepted.job_id)
    },
    onError: (error) => {
      toast.error(describeCatalogFailure(error))
    },
  })

  const discoverySchedule = contractDiscoverySchedule(schedules.data)

  const runContractDiscovery = useMutation({
    mutationFn: (scheduleId: string) =>
      api.post<RunNowWire>('/schedules/' + scheduleId + '/run-now'),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      if (result.job_id) {
        toast.success('Contract discovery queued')
        void navigate('/jobs/' + result.job_id)
        return
      }
      // A fire that enqueued nothing is not a failure, and saying "started" would be a lie.
      toast.info(result.note ?? result.outcome)
    },
    onError: (error) => {
      toast.error(describeCatalogFailure(error))
    },
  })

  /** Fills the range boxes from the registry floor and today. Called when the popover opens,
   *  from either of the two things that open it, so the form is never blank. */
  function prepareDiscoverRange(): void {
    if (underlying) {
      setDiscoverRange(discoverDefaults(underlying))
    }
  }

  function openDiscover(): void {
    prepareDiscoverRange()
    setDiscoverOpen(true)
  }

  if (!underlyings.isLoading && rows.length === 0) {
    return (
      <section className="flex min-h-0 flex-1 flex-col">
        <PageHeader title="Expiries" description="Pick an underlying to see its expiries." />
        <div className="p-5">
          <EmptyState
            title="No underlyings registered"
            description="Register an underlying first. Its expiries are discovered from the vendor, one request per 366 day window."
            action={
              <Button
                type="button"
                size="sm"
                onClick={() => {
                  void navigate('/underlyings')
                }}
              >
                Go to underlyings
              </Button>
            }
          />
        </div>
      </section>
    )
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="Expiries"
        description={
          underlying
            ? underlying.fyers_symbol +
              ', ' +
              formatInteger(underlying.expiry_count) +
              ' discovered, ' +
              formatInteger(underlying.contract_count) +
              ' contracts'
            : 'Pick an underlying.'
        }
        actions={
          <>
            <Popover
              open={discoverOpen}
              onOpenChange={(next) => {
                if (next) {
                  prepareDiscoverRange()
                }
                setDiscoverOpen(next)
              }}
            >
              <PopoverTrigger asChild>
                <Button type="button" size="sm" variant="outline">
                  Discover expiries
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-80">
                <div className="flex flex-col gap-3">
                  <p className="text-xs text-muted-foreground">
                    One request per 366 day window. The far end is clamped to the last day the
                    vendor serves, and the start is refused before the exchange data floor.
                  </p>
                  <div className="flex gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="discover-from" className="text-xs">
                        From
                      </Label>
                      <Input
                        id="discover-from"
                        type="date"
                        className="h-7 tabular-nums"
                        value={discoverRange?.range_from ?? ''}
                        onChange={(event) => {
                          setDiscoverRange((current) =>
                            current ? { ...current, range_from: event.target.value } : current,
                          )
                        }}
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="discover-to" className="text-xs">
                        To
                      </Label>
                      <Input
                        id="discover-to"
                        type="date"
                        className="h-7 tabular-nums"
                        value={discoverRange?.range_to ?? ''}
                        onChange={(event) => {
                          setDiscoverRange((current) =>
                            current ? { ...current, range_to: event.target.value } : current,
                          )
                        }}
                      />
                    </div>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    disabled={!underlying || !discoverRange || discover.isPending}
                    onClick={() => {
                      if (underlying && discoverRange) {
                        discover.mutate({
                          underlyingId: underlying.underlying_id,
                          range: discoverRange,
                        })
                      }
                    }}
                  >
                    {discover.isPending ? 'Queueing' : 'Queue discovery'}
                  </Button>
                </div>
              </PopoverContent>
            </Popover>

            {discoverySchedule ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={runContractDiscovery.isPending}
                onClick={() => {
                  runContractDiscovery.mutate(discoverySchedule.schedule_id)
                }}
                title={
                  'Fires ' +
                  discoverySchedule.name +
                  ' now: one contract list request per undiscovered expiry whose date has passed.'
                }
              >
                Run contract discovery
              </Button>
            ) : null}
          </>
        }
      >
        <Select
          value={underlying ? String(underlying.underlying_id) : undefined}
          onValueChange={(next) => {
            setSearchParams({ underlying: next })
          }}
        >
          <SelectTrigger size="sm" className="w-64">
            <SelectValue placeholder="Underlying" />
          </SelectTrigger>
          <SelectContent>
            {rows.map((row) => (
              <SelectItem key={row.underlying_id} value={String(row.underlying_id)}>
                {row.display_name} ({row.fyers_symbol})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-2">
          <Label htmlFor="expiry-from" className="text-xs text-muted-foreground">
            From
          </Label>
          <Input
            id="expiry-from"
            type="date"
            className="h-7 w-36 tabular-nums"
            value={rangeFrom}
            onChange={(event) => {
              setRangeFrom(event.target.value)
            }}
          />
          <Label htmlFor="expiry-to" className="text-xs text-muted-foreground">
            To
          </Label>
          <Input
            id="expiry-to"
            type="date"
            className="h-7 w-36 tabular-nums"
            value={rangeTo}
            onChange={(event) => {
              setRangeTo(event.target.value)
            }}
          />
        </div>

        {underlying && !underlying.mirrored ? (
          <Badge variant="destructive" className="text-[0.65rem]">
            no DuckDB mirror, repair it on the underlyings screen
          </Badge>
        ) : null}
      </PageHeader>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        <ExpirySelectTable
          rows={expiryRows}
          selection={selection}
          onSelectionChange={setSelection}
          isLoading={expiries.isLoading}
          emptyTitle="No expiries discovered yet"
          emptyDescription="Expiry dates come from the vendor, one request per 366 day window."
          emptyAction={
            <Button type="button" size="sm" onClick={openDiscover}>
              Discover expiries
            </Button>
          }
          footer={
            <>
              <span className="tabular-nums">
                {pluralise(expiryRows.length, 'expiry', 'expiries')} loaded
              </span>
              {expiries.hasNextPage ? (
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={expiries.isFetchingNextPage}
                  onClick={() => {
                    void expiries.fetchNextPage()
                  }}
                >
                  {expiries.isFetchingNextPage ? 'Loading' : 'Load more'}
                </Button>
              ) : null}
            </>
          }
        />
      </div>

      <SelectionBar
        rows={picked}
        onClear={() => {
          setSelection({})
        }}
        onDownload={() => {
          setSheetOpen(true)
        }}
      />

      {underlying ? (
        <DownloadSheet
          // Keyed by underlying so the sheet's own controls start from THAT underlying's declared
          // resolutions and open interest setting rather than the previous one's.
          key={underlying.underlying_id}
          open={sheetOpen}
          onOpenChange={setSheetOpen}
          underlying={underlying}
          rows={picked}
          onDiscoverContracts={
            discoverySchedule
              ? () => {
                  runContractDiscovery.mutate(discoverySchedule.schedule_id)
                }
              : undefined
          }
          onStarted={(accepted) => {
            setSheetOpen(false)
            setSelection({})
            void navigate('/jobs/' + accepted.job_id)
          }}
        />
      ) : null}
    </section>
  )
}
