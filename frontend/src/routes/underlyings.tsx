import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { toast } from 'sonner'

import { DataTable } from '@/components/common/DataTable'
import { PageHeader } from '@/components/common/PageHeader'
import {
  AddUnderlyingDialog,
  describeCatalogFailure,
  type UnderlyingWire,
} from '@/components/underlyings/AddUnderlyingDialog'
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
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Switch } from '@/components/ui/switch'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { AppTableFeatures } from '@/lib/tables/features'
import { formatCompact, formatDateTime, formatInteger } from '@/lib/format'

// The declared universe.
//
// Every row is two halves joined: what the user declared, from sqlite.underlying_registry, and
// what the store actually holds, from the DuckDB mirror. The route sends both plus a `mirrored`
// flag, and this screen shows that flag, because the two have silently drifted before: four
// seeded registry rows once had no mirror row and nine separate joins answered empty with no
// error anywhere. A row that says "mirror missing" is how that is found in seconds rather than
// from a blank chart.

export type { UnderlyingWire }

export interface StatusBadge {
  label: string
  tone: 'neutral' | 'warning'
  /** Shown as the title, so the badge is one word and the explanation is still reachable. */
  hint: string
}

/**
 * The badges one underlying carries.
 *
 * `mirrored` first when it is false: an unmirrored underlying is the one state on this screen
 * that makes every other screen wrong, and it is repaired from the row actions.
 */
export function underlyingStatusBadges(row: UnderlyingWire): StatusBadge[] {
  const badges: StatusBadge[] = []
  if (!row.mirrored) {
    badges.push({
      label: 'mirror missing',
      tone: 'warning',
      hint:
        'The registry row has no dim_underlying mirror. Every catalog and chart join reads the ' +
        'mirror, so this underlying answers empty everywhere. Repair it from the row actions.',
    })
  }
  if (!row.is_active) {
    badges.push({
      label: 'inactive',
      tone: 'warning',
      hint: 'Schedules skip this underlying. It can still be downloaded by hand.',
    })
  }
  if (row.is_builtin) {
    badges.push({
      label: 'builtin',
      tone: 'neutral',
      hint: 'One of the four seeded underlyings. It can be deactivated but not deleted.',
    })
  }
  if (row.resolved_root_echo === null) {
    badges.push({
      label: 'root unconfirmed',
      tone: 'neutral',
      hint: 'Fyers has not echoed this root back. Nothing is wrong yet; it was never probed.',
    })
  }
  return badges
}

/** The line under the name. Says what is held, not what was asked for. */
export function heldLine(row: UnderlyingWire): string {
  if (row.expiry_count === 0) {
    return 'No expiries discovered yet'
  }
  const span =
    row.first_expiry && row.last_expiry ? row.first_expiry + ' to ' + row.last_expiry : ''
  return (
    formatInteger(row.expiry_count) +
    ' expiries, ' +
    formatInteger(row.contract_count) +
    ' contracts' +
    (span ? ', ' + span : '')
  )
}

/** Spot history in one phrase, because an at the money band on the download sheet depends on it
 *  and this is where the user finds out it is missing. */
export function spotLine(row: UnderlyingWire): string {
  if (row.spot_bars === 0) {
    return 'no bars'
  }
  return (
    formatCompact(row.spot_bars) +
    ' bars' +
    (row.spot_last_ts ? ' to ' + row.spot_last_ts.slice(0, 10) : '')
  )
}

export default function UnderlyingsRoute() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [activeOnly, setActiveOnly] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<UnderlyingWire | null>(null)
  const [purgeData, setPurgeData] = useState(false)

  const params = { active_only: activeOnly }
  const underlyings = useQuery({
    queryKey: queryKeys.underlyings.list(params),
    queryFn: () => api.get<UnderlyingWire[]>('/underlyings', { query: params }),
  })

  const patch = useMutation({
    mutationFn: (input: { underlyingId: number; body: Record<string, unknown> }) =>
      api.patch<UnderlyingWire>('/underlyings/' + String(input.underlyingId), {
        body: input.body,
      }),
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: queryKeys.underlyings.all() })
      toast.success(
        row.display_name + (row.is_active ? ' is active' : ' is inactive') +
          (row.mirrored ? '' : '. The mirror is still missing.'),
      )
    },
    onError: (error) => {
      toast.error(describeCatalogFailure(error))
    },
  })

  const remove = useMutation({
    mutationFn: (input: { underlyingId: number; purge: boolean }) =>
      api.delete<void>('/underlyings/' + String(input.underlyingId), {
        query: { purge_data: input.purge },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.underlyings.all() })
      toast.success('Underlying removed')
      setPendingDelete(null)
      setPurgeData(false)
    },
    onError: (error) => {
      toast.error(describeCatalogFailure(error))
    },
  })

  const rows = underlyings.data ?? []
  const unmirrored = rows.filter((row) => !row.mirrored).length

  const columns = useMemo<Array<ColumnDef<AppTableFeatures, UnderlyingWire, unknown>>>(
    () => [
      {
        id: 'display_name',
        header: 'Underlying',
        cell: ({ row }) => (
          <div className="flex min-w-0 flex-col gap-0.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{row.original.display_name}</span>
              <span className="text-xs text-muted-foreground">{row.original.fyers_symbol}</span>
              {underlyingStatusBadges(row.original).map((badge) => (
                <Badge
                  key={badge.label}
                  variant={badge.tone === 'warning' ? 'destructive' : 'outline'}
                  className="text-[0.65rem]"
                  title={badge.hint}
                >
                  {badge.label}
                </Badge>
              ))}
            </div>
            <span className="text-xs text-muted-foreground">{heldLine(row.original)}</span>
          </div>
        ),
      },
      {
        id: 'root',
        header: 'Root',
        cell: ({ row }) => (
          <div className="flex flex-col gap-0.5 text-xs">
            <span className="tabular-nums">{row.original.root}</span>
            <span className="text-muted-foreground">
              {row.original.exchange} {row.original.segment} {row.original.instrument_kind}
            </span>
          </div>
        ),
      },
      {
        id: 'resolutions',
        header: 'Resolutions',
        cell: ({ row }) => (
          <div className="flex flex-col gap-0.5 text-xs">
            <span className="tabular-nums">{row.original.default_resolutions.join(', ')}</span>
            <span className="text-muted-foreground">
              {row.original.include_oi ? 'open interest on' : 'open interest off'}
            </span>
          </div>
        ),
      },
      {
        id: 'spot',
        header: 'Underlying history',
        cell: ({ row }) => (
          <span className="text-xs tabular-nums text-muted-foreground">
            {spotLine(row.original)}
          </span>
        ),
      },
      {
        id: 'resolved_at',
        header: 'Root confirmed',
        cell: ({ row }) => (
          <span className="text-xs tabular-nums text-muted-foreground">
            {row.original.resolved_root_echo
              ? row.original.resolved_root_echo +
                (row.original.resolved_at ? ' on ' + formatDateTime(row.original.resolved_at) : '')
              : 'never probed'}
          </span>
        ),
      },
      {
        id: 'actions',
        header: '',
        size: 48,
        cell: ({ row }) => (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={(event) => {
                  event.stopPropagation()
                }}
              >
                Actions
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onSelect={() => {
                  void navigate('/expiries?underlying=' + String(row.original.underlying_id))
                }}
              >
                Open expiries
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => {
                  patch.mutate({
                    underlyingId: row.original.underlying_id,
                    body: { is_active: !row.original.is_active },
                  })
                }}
              >
                {row.original.is_active ? 'Deactivate' : 'Activate'}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => {
                  // A patch re-mirrors dim_underlying every time, so writing the values back
                  // unchanged is what repairs a missing mirror. Nothing else on this screen can.
                  patch.mutate({
                    underlyingId: row.original.underlying_id,
                    body: { display_name: row.original.display_name },
                  })
                }}
              >
                Repair the DuckDB mirror
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={row.original.is_builtin}
                onSelect={() => {
                  setPendingDelete(row.original)
                  setPurgeData(false)
                }}
              >
                Remove
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ],
    [navigate, patch],
  )

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="Underlyings"
        description="What this app downloads for, and what it already holds."
        actions={
          <Button
            type="button"
            size="sm"
            onClick={() => {
              setAddOpen(true)
            }}
          >
            Add underlying
          </Button>
        }
      >
        <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
          <Switch checked={activeOnly} onCheckedChange={setActiveOnly} aria-label="Active only" />
          <span>Active only</span>
        </label>
        {unmirrored > 0 ? (
          <span className="text-xs text-destructive">
            {formatInteger(unmirrored)} of these have no DuckDB mirror and will answer empty on
            every other screen.
          </span>
        ) : null}
      </PageHeader>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        <DataTable<UnderlyingWire>
          data={rows}
          columns={columns}
          getRowId={(row) => String(row.underlying_id)}
          isLoading={underlyings.isLoading}
          onRowClick={(row) => {
            void navigate('/expiries?underlying=' + String(row.underlying_id))
          }}
          emptyTitle="No underlyings registered"
          emptyDescription="Add one to start discovering its expiries."
          emptyAction={
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setAddOpen(true)
              }}
            >
              Add underlying
            </Button>
          }
        />
      </div>

      <AddUnderlyingDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={(row) => {
          void navigate('/expiries?underlying=' + String(row.underlying_id))
        }}
      />

      <AlertDialog
        open={pendingDelete !== null}
        onOpenChange={(next) => {
          if (!next) {
            setPendingDelete(null)
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {pendingDelete?.display_name}</AlertDialogTitle>
            <AlertDialogDescription>
              The registry row and its mirror go. Downloaded candles stay unless the box below is
              ticked, and a removal is refused while a job for this underlying is running.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox
              checked={purgeData}
              onCheckedChange={(next) => {
                setPurgeData(next === true)
              }}
              aria-label="Delete the downloaded data as well"
            />
            <span>Delete the downloaded candles as well. This cannot be undone.</span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingDelete) {
                  remove.mutate({
                    underlyingId: pendingDelete.underlying_id,
                    purge: purgeData,
                  })
                }
              }}
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
