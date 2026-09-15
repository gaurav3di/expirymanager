import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { resolutionChoices } from '@/components/download/DownloadSheet'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatInteger } from '@/lib/format'

// Add an underlying the app did not ship with.
//
// The flow is resolve, then register, and it is two steps because the derivative root a user
// types is not the symbol the expired endpoints accept. BANKNIFTY options are filed under the
// cash ticker NSE:NIFTYBANK-INDEX, and nothing in the derivative symbol says so. The resolve step
// walks the instrument master to find that join and then spends exactly one governed request to
// hear the vendor echo the root back. Registering without that echo is registering a guess.
//
// MCX is refused here, before the resolve request is spent. Seven MCX underlying forms were
// probed against the live API on 2026-09-09 and all seven answered HTTP 422 while
// BSE:SENSEX-INDEX answered 200 in the same run, so the expired endpoints do not serve MCX at
// all. Offering it would spend budget to be told that one 422 at a time.

// ---------------------------------------------------------------------------
// The wire shapes
// ---------------------------------------------------------------------------

/** `UnderlyingOut` from api/schemas/catalog.py, in full.
 *
 *  Wider than the `Underlying` in lib/api/types.ts, which predates the route: `mirrored`, the two
 *  life windows, the spot contract id and the resolve echo are all sent and all shown. */
export interface UnderlyingWire {
  underlying_id: number
  fyers_symbol: string
  root: string
  exchange: string
  segment: string
  instrument_kind: string
  display_name: string
  data_from: string
  default_resolutions: string[]
  include_oi: boolean
  option_life_days: number
  future_life_days: number
  spot_contract_id: number
  resolved_root_echo: string | null
  resolved_at: string | null
  is_builtin: boolean
  is_active: boolean
  /** False when the registry row has no DuckDB mirror. Every catalog and chart join reads the
   *  mirror, so an unmirrored underlying answers empty everywhere with no error anywhere. */
  mirrored: boolean
  expiry_count: number
  contract_count: number
  first_expiry: string | null
  last_expiry: string | null
  spot_bars: number
  spot_last_ts: string | null
}

export interface ResolveCandidateWire {
  fyers_symbol: string
  root: string
  exchange: string
  segment: string
  instrument_kind: string
  display_name: string
  under_fytoken: string
  fo_contract_count: number
  source: string
  already_registered: boolean
}

/** A root that matched and cannot be registered. Reported rather than dropped: the measured case
 *  is a root starting with a digit, which the symbol parser does not accept, and hiding it would
 *  let a user add an underlying that fails much later inside the pipeline. */
export interface ResolveRejectionWire {
  root: string
  exchange: string
  reason: string
  fo_contract_count: number
}

export interface ResolveProbeWire {
  attempted: boolean
  symbol: string | null
  root_echo: string | null
  expiry_count: number | null
  range_from: string | null
  range_to: string | null
  reason: string | null
}

export interface ResolveResponseWire {
  candidates: ResolveCandidateWire[]
  rejected: ResolveRejectionWire[]
  probe: ResolveProbeWire
}

// ---------------------------------------------------------------------------
// What is refused before a request is spent
// ---------------------------------------------------------------------------

/** Kept in step with pipeline/handlers/candle_chunk.MCX_REFUSAL, which is what the backend
 *  answers with when an MCX symbol reaches it anyway. */
export const MCX_NOT_SERVED =
  'MCX is not served by the expired F and O endpoints. Every MCX underlying form probed on ' +
  '2026-09-09 answered HTTP 422 while BSE:SENSEX-INDEX answered 200 in the same run, so this ' +
  'search would spend a request to receive an error. Search for an NSE or a BSE underlying.'

/** The backend's own ceiling, from ResolveRequest.query. */
export const MAX_QUERY_LENGTH = 64

export interface QueryGuard {
  ok: boolean
  /** The reason, shown under the input. Null when the query may be sent. */
  message: string | null
}

/** Whether this query is worth a governed request. */
export function guardQuery(raw: string): QueryGuard {
  const query = raw.trim()
  if (query.length === 0) {
    return { ok: false, message: null }
  }
  if (query.length > MAX_QUERY_LENGTH) {
    return {
      ok: false,
      message: 'A search is at most ' + String(MAX_QUERY_LENGTH) + ' characters.',
    }
  }
  const upper = query.toUpperCase()
  if (upper === 'MCX' || upper.startsWith('MCX:')) {
    return { ok: false, message: MCX_NOT_SERVED }
  }
  return { ok: true, message: null }
}

/** The sentence to show when a resolve or a create is refused. The backend writes these to be
 *  shown, so they are shown; only the shape around them is added. */
export function describeCatalogFailure(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : String(error)
  }
  if (error.isRateLimited) {
    const wait = error.retryAfterSeconds
    return (
      error.message +
      (wait === null ? '' : ' Try again in ' + formatInteger(wait) + ' seconds.')
    )
  }
  return error.message
}

/** What the probe line says. The echo is the authoritative root, so when it is absent the screen
 *  says the search was not confirmed rather than implying it was. */
export function probeLine(probe: ResolveProbeWire): string {
  if (!probe.attempted) {
    return probe.reason === 'needs_reauth'
      ? 'The vendor was not asked to confirm the root: the Fyers session has expired. The ' +
          'matches below come from the stored instrument master only.'
      : 'The vendor was not asked to confirm the root. The matches below come from the stored ' +
          'instrument master only.'
  }
  const echo = probe.root_echo ?? 'nothing'
  const count = probe.expiry_count === null ? '' : ', ' + formatInteger(probe.expiry_count) + ' expiries'
  const window =
    probe.range_from && probe.range_to ? ' over ' + probe.range_from + ' to ' + probe.range_to : ''
  return 'Fyers echoed ' + echo + ' for ' + (probe.symbol ?? 'the symbol') + count + window + '.'
}

// ---------------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------------

interface CreateBody {
  fyers_symbol: string
  display_name: string
  default_resolutions: string[]
  include_oi: boolean
  option_life_days: number
  future_life_days: number
}

export interface AddUnderlyingDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated?: (underlying: UnderlyingWire) => void
}

export function AddUnderlyingDialog({
  open,
  onOpenChange,
  onCreated,
}: AddUnderlyingDialogProps) {
  const client = useQueryClient()
  const [query, setQuery] = useState('')
  const [resolved, setResolved] = useState<ResolveResponseWire | null>(null)
  const [chosen, setChosen] = useState<ResolveCandidateWire | null>(null)
  const [displayName, setDisplayName] = useState('')
  const [resolutions, setResolutions] = useState<string[]>(['1', '5', '15', '60'])
  const [includeOi, setIncludeOi] = useState(true)
  const [optionLifeDays, setOptionLifeDays] = useState(200)
  const [futureLifeDays, setFutureLifeDays] = useState(400)
  const [failure, setFailure] = useState<string | null>(null)

  const guard = guardQuery(query)
  const choices = resolutionChoices(resolutions)

  const resolve = useMutation({
    mutationFn: (value: string) =>
      api.post<ResolveResponseWire>('/underlyings/resolve', { body: { query: value } }),
    onSuccess: (result) => {
      setResolved(result)
      setFailure(null)
      const first = result.candidates.find((candidate) => !candidate.already_registered) ?? null
      setChosen(first)
      setDisplayName(first?.display_name ?? '')
    },
    onError: (error) => {
      setResolved(null)
      setChosen(null)
      setFailure(describeCatalogFailure(error))
    },
  })

  const create = useMutation({
    mutationFn: (body: CreateBody) => api.post<UnderlyingWire>('/underlyings', { body }),
    onSuccess: (underlying) => {
      void client.invalidateQueries({ queryKey: queryKeys.underlyings.all() })
      toast.success(underlying.display_name + ' registered')
      onCreated?.(underlying)
      reset()
      onOpenChange(false)
    },
    onError: (error) => {
      setFailure(describeCatalogFailure(error))
    },
  })

  function reset(): void {
    setQuery('')
    setResolved(null)
    setChosen(null)
    setDisplayName('')
    setFailure(null)
  }

  function submitQuery(): void {
    if (!guard.ok) {
      // The MCX refusal is the reason this is a guard and not a disabled button: the user has to
      // read why, and no request is spent to tell them.
      setFailure(guard.message)
      return
    }
    setFailure(null)
    resolve.mutate(query.trim())
  }

  function submitCreate(): void {
    if (!chosen) {
      return
    }
    create.mutate({
      fyers_symbol: chosen.fyers_symbol,
      display_name: displayName.trim() || chosen.display_name || chosen.root,
      default_resolutions: resolutions,
      include_oi: includeOi,
      option_life_days: optionLifeDays,
      future_life_days: futureLifeDays,
    })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          reset()
        }
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add an underlying</DialogTitle>
          <DialogDescription>
            Search the stored instrument master for a derivative root, confirm it against Fyers,
            then register it. NSE and BSE only.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="underlying-query">Root, cash ticker or full symbol</Label>
            <div className="flex gap-2">
              <Input
                id="underlying-query"
                value={query}
                placeholder="NIFTYNXT50"
                autoComplete="off"
                onChange={(event) => {
                  setQuery(event.target.value)
                  setFailure(null)
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    submitQuery()
                  }
                }}
              />
              <Button
                type="button"
                onClick={submitQuery}
                disabled={query.trim().length === 0 || resolve.isPending}
              >
                {resolve.isPending ? 'Searching' : 'Search'}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              One governed expiry dates request is spent to confirm the root. Nothing is written
              until Register.
            </p>
          </div>

          {failure ? <p className="text-xs text-destructive">{failure}</p> : null}

          {resolved ? (
            <div className="flex flex-col gap-3">
              <p className="text-xs text-muted-foreground">{probeLine(resolved.probe)}</p>

              {resolved.candidates.length === 0 ? (
                <p className="text-sm">No underlying in the instrument master matches that.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {resolved.candidates.map((candidate) => {
                    const selected = chosen?.fyers_symbol === candidate.fyers_symbol
                    return (
                      <li key={candidate.fyers_symbol + candidate.root}>
                        <button
                          type="button"
                          disabled={candidate.already_registered}
                          onClick={() => {
                            setChosen(candidate)
                            setDisplayName(candidate.display_name)
                          }}
                          className={
                            'flex w-full flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 ' +
                            (selected ? 'border-primary bg-muted/60 ' : 'hover:bg-muted/40 ') +
                            (candidate.already_registered ? 'cursor-not-allowed opacity-60' : '')
                          }
                        >
                          <span className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-medium">{candidate.fyers_symbol}</span>
                            <Badge variant="outline" className="text-[0.65rem]">
                              {candidate.root}
                            </Badge>
                            <Badge variant="outline" className="text-[0.65rem]">
                              {candidate.exchange} {candidate.segment}
                            </Badge>
                            {candidate.already_registered ? (
                              <Badge variant="secondary" className="text-[0.65rem]">
                                already registered
                              </Badge>
                            ) : null}
                          </span>
                          <span className="text-xs text-muted-foreground tabular-nums">
                            {formatInteger(candidate.fo_contract_count)} contracts in the master,
                            under_fytoken {candidate.under_fytoken}
                          </span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}

              {resolved.rejected.length > 0 ? (
                <div className="flex flex-col gap-1 rounded-md border px-3 py-2">
                  <span className="text-xs font-medium">Matched but cannot be registered</span>
                  {resolved.rejected.map((rejection) => (
                    <span
                      key={rejection.root + rejection.exchange}
                      className="text-xs text-muted-foreground"
                    >
                      {rejection.root} ({rejection.exchange}): {rejection.reason}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}

          {chosen ? (
            <div className="flex flex-col gap-3 border-t pt-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="underlying-name">Display name</Label>
                <Input
                  id="underlying-name"
                  value={displayName}
                  onChange={(event) => {
                    setDisplayName(event.target.value)
                  }}
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label>Default resolutions</Label>
                <div className="flex flex-wrap gap-x-4 gap-y-2">
                  {choices.map((choice) => (
                    <label
                      key={choice.code}
                      className="flex cursor-pointer items-center gap-2 text-sm"
                    >
                      <Checkbox
                        checked={resolutions.includes(choice.code)}
                        onCheckedChange={(next) => {
                          setResolutions((current) =>
                            next === true
                              ? [...current, choice.code]
                              : current.filter((item) => item !== choice.code),
                          )
                        }}
                        aria-label={choice.label}
                      />
                      <span className="tabular-nums">{choice.label}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div className="flex flex-wrap items-end gap-6">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="option-life">Option life, days</Label>
                  <Input
                    id="option-life"
                    type="number"
                    min={1}
                    max={3650}
                    className="w-28 tabular-nums"
                    value={optionLifeDays}
                    onChange={(event) => {
                      setOptionLifeDays(Number(event.target.value))
                    }}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="future-life">Future life, days</Label>
                  <Input
                    id="future-life"
                    type="number"
                    min={1}
                    max={3650}
                    className="w-28 tabular-nums"
                    value={futureLifeDays}
                    onChange={(event) => {
                      setFutureLifeDays(Number(event.target.value))
                    }}
                  />
                </div>
                <label className="flex cursor-pointer items-center gap-2 pb-2 text-sm">
                  <Switch
                    checked={includeOi}
                    onCheckedChange={setIncludeOi}
                    aria-label="Download open interest"
                  />
                  <span>Open interest</span>
                </label>
              </div>
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              onOpenChange(false)
            }}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={submitCreate}
            disabled={!chosen || resolutions.length === 0 || create.isPending}
          >
            {create.isPending ? 'Registering' : 'Register'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default AddUnderlyingDialog
