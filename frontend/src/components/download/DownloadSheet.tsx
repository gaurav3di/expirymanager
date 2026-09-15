import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'
import { toast } from 'sonner'

import {
  PlanPreviewCard,
  describePlanFailure,
  type DownloadAcceptedBody,
  type PlanFailure,
  type PlanPreviewBody,
} from '@/components/download/PlanPreview'
import type { ExpiryRow } from '@/components/download/ExpirySelectTable'
import { summariseSelection } from '@/components/download/SelectionBar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatInteger, pluralise } from '@/lib/format'

// The download sheet: what to fetch for the ticked expiries, and what it will cost.
//
// The sheet prices itself. Every change to a control re-runs POST /downloads/plan, which is
// answered entirely from local state and spends no Fyers requests, so the estimate on screen is
// always the estimate for the controls on screen. That is the whole point of the plan and commit
// split, and it is why Start can carry `confirm_requests` and mean it.
//
// Two things here are refusals rather than fallbacks, which is deliberate.
//
//  - An at the money band needs the underlying's own bars for the expiry day. When those are not
//    stored the option is rendered DISABLED with the reason, and a one click remedy downloads the
//    underlying history. Quietly widening to every strike instead would turn a twenty request
//    sheet into a four hundred and eighty two request one without saying so.
//  - A sheet that asks for nothing new is not started. The ledger already holds those windows.

// ---------------------------------------------------------------------------
// Resolutions
// ---------------------------------------------------------------------------

export interface ResolutionChoice {
  /** The Fyers code, which is what the plan request carries. */
  code: string
  label: string
  /** Second resolutions exist only inside the last 30 trading days, and that data is gone once
   *  the window closes. Offered, because an expiry inside the window is a real case, but said. */
  isSeconds: boolean
}

/**
 * The resolution catalog, mirroring the `dim_resolution` seed in db/duck_schema.sql.
 *
 * There is no endpoint that lists it. Rather than leave the sheet able to ask only for the codes
 * an underlying was registered with, the seed is mirrored here and any code the backend sends
 * that is not in this list is merged in by `resolutionChoices`, so a row added to the seed still
 * reaches this screen instead of disappearing from it.
 */
export const RESOLUTION_CATALOG: readonly ResolutionChoice[] = [
  { code: '5S', label: '5 seconds', isSeconds: true },
  { code: '1', label: '1 minute', isSeconds: false },
  { code: '2', label: '2 minutes', isSeconds: false },
  { code: '3', label: '3 minutes', isSeconds: false },
  { code: '5', label: '5 minutes', isSeconds: false },
  { code: '10', label: '10 minutes', isSeconds: false },
  { code: '15', label: '15 minutes', isSeconds: false },
  { code: '20', label: '20 minutes', isSeconds: false },
  { code: '30', label: '30 minutes', isSeconds: false },
  { code: '45', label: '45 minutes', isSeconds: false },
  { code: '60', label: '1 hour', isSeconds: false },
  { code: '120', label: '2 hours', isSeconds: false },
  { code: '180', label: '3 hours', isSeconds: false },
  { code: '240', label: '4 hours', isSeconds: false },
  { code: 'D', label: '1 day', isSeconds: false },
]

const SECONDS_CODE_PATTERN = /^\d+S$/

/** The catalog plus any code the backend already uses that this build has not heard of. */
export function resolutionChoices(declared: readonly string[]): ResolutionChoice[] {
  const known = new Set(RESOLUTION_CATALOG.map((item) => item.code))
  const extra = declared
    .map((code) => code.trim().toUpperCase())
    .filter((code) => code.length > 0 && !known.has(code))
    .filter((code, index, all) => all.indexOf(code) === index)
    .map<ResolutionChoice>((code) => ({
      code,
      label: code,
      isSeconds: SECONDS_CODE_PATTERN.test(code),
    }))
  return [...RESOLUTION_CATALOG, ...extra]
}

// ---------------------------------------------------------------------------
// The sheet's state
// ---------------------------------------------------------------------------

export type InstrumentClass = 'FUT' | 'OPT' | 'BOTH'
export type OptionRight = 'CE' | 'PE'

export type StrikeScopeValue =
  | { mode: 'all' }
  | { mode: 'atm_band'; steps: number }
  | { mode: 'explicit'; strikes: number[] }

export interface DownloadSheetValue {
  resolutions: string[]
  instrumentClass: InstrumentClass
  optionTypes: OptionRight[]
  strikeScope: StrikeScopeValue
  includeOi: boolean
  forceRefresh: boolean
}

/** The fields of an underlying this sheet reads. Structurally satisfied by the full registry row
 *  the screens hold, so nothing has to be mapped on the way in. */
export interface SheetUnderlying {
  underlying_id: number
  fyers_symbol: string
  display_name: string
  exchange: string
  data_from: string
  default_resolutions: string[]
  include_oi: boolean
  spot_bars: number
  spot_last_ts: string | null
}

export function defaultSheetValue(underlying: SheetUnderlying): DownloadSheetValue {
  return {
    // What the underlying was registered with. The user chose those codes once and a sheet that
    // ignored them would ask the question again on every visit.
    resolutions: underlying.default_resolutions.length > 0 ? [...underlying.default_resolutions] : ['1'],
    instrumentClass: 'OPT',
    optionTypes: ['CE', 'PE'],
    strikeScope: { mode: 'all' },
    includeOi: underlying.include_oi,
    forceRefresh: false,
  }
}

/** Problems the backend would answer 422 for. Caught here so the sheet says what is wrong next to
 *  the control instead of spending a round trip to be told. */
export function sheetProblems(value: DownloadSheetValue): string[] {
  const problems: string[] = []
  if (value.resolutions.length === 0) {
    problems.push('Choose at least one resolution.')
  }
  if (value.instrumentClass !== 'FUT' && value.optionTypes.length === 0) {
    problems.push('Choose calls, puts or both.')
  }
  if (value.strikeScope.mode === 'atm_band' && value.strikeScope.steps < 1) {
    problems.push('An at the money band needs at least one step each side.')
  }
  if (value.strikeScope.mode === 'explicit' && value.strikeScope.strikes.length === 0) {
    problems.push('List at least one strike, or switch back to every strike.')
  }
  return problems
}

/** Reads a typed strike list. Commas, spaces and newlines all separate, because all three are
 *  what comes out of a spreadsheet. */
export function parseStrikes(text: string): number[] {
  const seen: number[] = []
  for (const token of text.split(/[\s,]+/)) {
    if (!token) {
      continue
    }
    const value = Number(token)
    if (Number.isFinite(value) && !seen.includes(value)) {
      seen.push(value)
    }
  }
  return seen
}

// ---------------------------------------------------------------------------
// Spot history, which an at the money band depends on
// ---------------------------------------------------------------------------

export interface SpotAvailability {
  available: boolean
  /** Why the band cannot be centred. Null when it can. */
  reason: string | null
  /** The last IST day the underlying's own series holds a bar for. */
  lastSpotDay: string | null
}

/**
 * Whether an at the money band can be centred for every selected expiry.
 *
 * The band is centred on the underlying's own close for the expiry day, so the question is not
 * "is there spot history" but "does it reach these days". Both facts are read off the registry
 * row the list already returned, which is derived from `contract_bounds` for the spot contract.
 *
 * This is a necessary condition and not a sufficient one: a hole in the middle of the series
 * still fails when the planner looks for that one day, and that answer arrives as
 * `strike_scope_needs_spot` with the same remedy attached. Two paths, one remedy.
 */
export function spotAvailability(
  underlying: SheetUnderlying,
  expiryDates: readonly string[],
): SpotAvailability {
  if (underlying.spot_bars <= 0 || !underlying.spot_last_ts) {
    return {
      available: false,
      reason:
        'No bars are stored for ' +
        underlying.fyers_symbol +
        ' itself, so the at the money strike for an expiry day cannot be found.',
      lastSpotDay: null,
    }
  }

  const lastSpotDay = underlying.spot_last_ts.slice(0, 10)
  const past = expiryDates.filter((date) => date > lastSpotDay)
  if (past.length > 0) {
    return {
      available: false,
      reason:
        'The stored history for ' +
        underlying.fyers_symbol +
        ' ends on ' +
        lastSpotDay +
        ', and ' +
        pluralise(past.length, 'selected expiry', 'selected expiries') +
        ' fall after it.',
      lastSpotDay,
    }
  }
  return { available: true, reason: null, lastSpotDay }
}

// ---------------------------------------------------------------------------
// Request bodies
// ---------------------------------------------------------------------------

export interface PlanRequestBody {
  underlying_id: number
  expiry_dates: string[]
  resolutions: string[]
  instrument_class: InstrumentClass
  option_types: OptionRight[]
  strike_scope: StrikeScopeValue
  include_oi: boolean
  force_refresh: boolean
  range_from: string | null
  range_to: string | null
  include_spot: boolean
}

export interface DownloadRequestBody extends PlanRequestBody {
  confirm_requests: number
  defer_to_tomorrow: boolean
}

export function buildPlanRequest(
  value: DownloadSheetValue,
  context: { underlyingId: number; expiryDates: readonly string[] },
): PlanRequestBody {
  return {
    underlying_id: context.underlyingId,
    expiry_dates: [...context.expiryDates],
    resolutions: [...value.resolutions],
    instrument_class: value.instrumentClass,
    // Sent even for a futures sheet: the backend ignores it there, and dropping it would make
    // two request shapes out of one form.
    option_types: [...value.optionTypes],
    strike_scope: value.strikeScope,
    include_oi: value.includeOi,
    force_refresh: value.forceRefresh,
    range_from: null,
    range_to: null,
    include_spot: false,
  }
}

/**
 * A date that cannot be an expiry in the catalog.
 *
 * `PlanRequest` requires at least one expiry date and the planner uses the list only to bound the
 * spot window when no explicit range is given. Both bounds are explicit in the remedy below, so
 * this placeholder is never read; what it does is guarantee that no contract of any real expiry
 * is planned, which is what makes the remedy a spot only sheet. The same trick, for the same
 * reason, is what scheduler/jobs_def.run_underlying_history does with today's date.
 *
 * Before the NSE data floor of 2022-01-03 and the BSE floor of 2023-08-07, so it can never
 * collide with a discovered expiry.
 */
export const SPOT_SHEET_PLACEHOLDER_DATE = '1970-01-01'

/** Resolutions the spot series can actually be asked for. Second resolutions are dropped the way
 *  the underlying_history schedule drops them: their window is the last 30 trading days, and a
 *  spot backfill is not that. */
export function spotResolutions(
  value: DownloadSheetValue,
  underlying: SheetUnderlying,
): string[] {
  const usable = (codes: readonly string[]) =>
    codes
      .map((code) => code.trim().toUpperCase())
      .filter((code) => code.length > 0 && !SECONDS_CODE_PATTERN.test(code))
  const chosen = usable(value.resolutions)
  if (chosen.length > 0) {
    return chosen
  }
  const declared = usable(underlying.default_resolutions)
  return declared.length > 0 ? declared : ['1']
}

/**
 * The one click remedy for a missing underlying history.
 *
 * Spot bars only, over exactly the span of the selected expiries, which is the span the at the
 * money band needs a close for. It is priced and confirmed through the same preview as any other
 * sheet, so the user sees what the remedy costs before it runs.
 */
export function buildSpotHistoryRequest(
  value: DownloadSheetValue,
  underlying: SheetUnderlying,
  expiryDates: readonly string[],
): PlanRequestBody {
  const sorted = [...expiryDates].sort()
  return {
    underlying_id: underlying.underlying_id,
    expiry_dates: [SPOT_SHEET_PLACEHOLDER_DATE],
    resolutions: spotResolutions(value, underlying),
    instrument_class: 'OPT',
    option_types: ['CE', 'PE'],
    strike_scope: { mode: 'all' },
    include_oi: false,
    force_refresh: false,
    range_from: sorted[0] ?? underlying.data_from,
    range_to: sorted[sorted.length - 1] ?? underlying.data_from,
    include_spot: true,
  }
}

/** True when two request bodies would price identically, so the sheet does not spend a round trip
 *  re-asking a question it already has the answer to. */
export function sameRequest(left: PlanRequestBody | null, right: PlanRequestBody): boolean {
  return left !== null && JSON.stringify(left) === JSON.stringify(right)
}

// ---------------------------------------------------------------------------
// The sheet
// ---------------------------------------------------------------------------

/** Long enough that dragging the step count from 1 to 12 is one price and not twelve. */
const PRICE_DEBOUNCE_MS = 500

export interface DownloadSheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  underlying: SheetUnderlying
  rows: readonly ExpiryRow[]
  /** Fires a contract discovery run. The planner refuses to price a sheet on which no selected
   *  expiry has contracts, and this is the only thing in the system that creates them, so the
   *  refusal is a dead end without it. Omitted when the installation has no such schedule. */
  onDiscoverContracts?: () => void
  /** Called with the accepted job so the screen can navigate to it. */
  onStarted: (accepted: DownloadAcceptedBody) => void
}

export function DownloadSheet({
  open,
  onOpenChange,
  underlying,
  rows,
  onDiscoverContracts,
  onStarted,
}: DownloadSheetProps) {
  const client = useQueryClient()
  const [value, setValue] = useState<DownloadSheetValue>(() => defaultSheetValue(underlying))
  const [strikeText, setStrikeText] = useState('')
  const [deferToTomorrow, setDeferToTomorrow] = useState(false)
  const [remedyMode, setRemedyMode] = useState(false)
  // The estimate is held WITH the body it priced. A preview that does not belong to the controls
  // on screen is not shown and cannot be started: the whole contract of this sheet is that the
  // number the user confirms is the number they were shown for what they are looking at.
  const [priced, setPriced] = useState<{ body: PlanRequestBody; preview: PlanPreviewBody } | null>(
    null,
  )
  const [failure, setFailure] = useState<PlanFailure | null>(null)
  /** The last body actually sent, so a refused price is not retried in a loop. */
  const attemptedRef = useRef<PlanRequestBody | null>(null)

  const expiryDates = useMemo(() => rows.map((row) => row.expiry_date).sort(), [rows])
  const summary = useMemo(() => summariseSelection(rows), [rows])
  const choices = useMemo(
    () => resolutionChoices(underlying.default_resolutions),
    [underlying.default_resolutions],
  )
  const spot = useMemo(
    () => spotAvailability(underlying, expiryDates),
    [underlying, expiryDates],
  )
  // The remedy sheet does not use these controls: its resolutions come from `spotResolutions`,
  // which always yields at least one code, so a half filled download sheet must not block it.
  const problems = remedyMode ? [] : sheetProblems(value)

  const requestBody = useMemo(
    () =>
      remedyMode
        ? buildSpotHistoryRequest(value, underlying, expiryDates)
        : buildPlanRequest(value, {
            underlyingId: underlying.underlying_id,
            expiryDates,
          }),
    [remedyMode, value, underlying, expiryDates],
  )

  const price = useMutation({
    mutationFn: (body: PlanRequestBody) => api.post<PlanPreviewBody>('/downloads/plan', { body }),
    onSuccess: (result, body) => {
      setPriced({ body, preview: result })
      setFailure(null)
    },
    onError: (error) => {
      setPriced(null)
      setFailure(describePlanFailure(error))
    },
  })

  // Shown only while it still describes the controls on screen. An edit that has not been priced
  // yet blanks the card, which is also what disables Start.
  const currentPreview =
    priced && sameRequest(priced.body, requestBody) && problems.length === 0
      ? priced.preview
      : null

  const start = useMutation({
    mutationFn: (body: DownloadRequestBody) =>
      api.post<DownloadAcceptedBody>('/downloads', { body }),
    onSuccess: (accepted) => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      void client.invalidateQueries({ queryKey: queryKeys.underlyings.all() })
      void client.invalidateQueries({ queryKey: queryKeys.system.budget() })
      toast.success(
        accepted.deferred
          ? 'Queued for tomorrow: ' + formatInteger(accepted.total_tasks) + ' tasks'
          : 'Download started: ' + formatInteger(accepted.total_tasks) + ' tasks',
      )
      resetPricing()
      onStarted(accepted)
    },
    onError: (error) => {
      const described = describePlanFailure(error)
      setFailure(described)
      if (described.repriceable) {
        // A refused commit leaves the user holding a price that is no longer true. Re-pricing
        // immediately is the only way the next click can be honest.
        attemptedRef.current = null
        price.mutate(requestBody)
      }
    },
  })

  // Price on every settled change. The call spends no Fyers requests, so the cost of keeping the
  // estimate in step with the controls is one local query.
  const priceMutate = price.mutate
  useEffect(() => {
    if (!open || expiryDates.length === 0 || problems.length > 0) {
      return
    }
    if (sameRequest(attemptedRef.current, requestBody)) {
      return
    }
    const timer = window.setTimeout(() => {
      attemptedRef.current = requestBody
      priceMutate(requestBody)
    }, PRICE_DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [open, expiryDates.length, problems.length, requestBody, priceMutate])

  /** A sheet opened again starts from a blank estimate rather than the last one.
   *
   *  Called from the close handler and from a successful start rather than from an effect on
   *  `open`, because an effect that sets state is a second render for something that has a single
   *  cause: the sheet closed. */
  function resetPricing(): void {
    setPriced(null)
    setFailure(null)
    setRemedyMode(false)
    setDeferToTomorrow(false)
    attemptedRef.current = null
  }

  function update(patch: Partial<DownloadSheetValue>): void {
    setValue((current) => ({ ...current, ...patch }))
  }

  function toggleResolution(code: string, checked: boolean): void {
    update({
      resolutions: checked
        ? [...value.resolutions, code]
        : value.resolutions.filter((item) => item !== code),
    })
  }

  function toggleRight(right: OptionRight, checked: boolean): void {
    update({
      optionTypes: checked
        ? [...value.optionTypes, right]
        : value.optionTypes.filter((item) => item !== right),
    })
  }

  function setScopeMode(mode: StrikeScopeValue['mode']): void {
    if (mode === 'all') {
      update({ strikeScope: { mode: 'all' } })
      return
    }
    if (mode === 'atm_band') {
      update({ strikeScope: { mode: 'atm_band', steps: 10 } })
      return
    }
    update({ strikeScope: { mode: 'explicit', strikes: parseStrikes(strikeText) } })
  }

  const remedyAction =
    failure?.remedy === 'discover_contracts' ? (
      onDiscoverContracts ? (
        <Button type="button" size="sm" variant="outline" onClick={onDiscoverContracts}>
          Run contract discovery now
        </Button>
      ) : (
        <span className="text-xs">
          This installation has no contract discovery schedule, so there is nothing to fire. Add
          one on the schedules screen.
        </span>
      )
    ) : failure?.remedy === 'spot_history' ? (
      <Button type="button" size="sm" variant="outline" onClick={() => { enterRemedy() }}>
        Download the underlying history first
      </Button>
    ) : failure?.remedy === 'reconnect' ? (
      <Button type="button" size="sm" variant="outline" asChild>
        <a href="/settings?tab=broker">Reconnect Fyers</a>
      </Button>
    ) : null

  function enterRemedy(): void {
    setRemedyMode(true)
    setPriced(null)
    setFailure(null)
    attemptedRef.current = null
  }

  function leaveRemedy(): void {
    setRemedyMode(false)
    setPriced(null)
    setFailure(null)
    attemptedRef.current = null
  }

  /** The card only offers Start while the preview it is showing belongs to `requestBody`, so the
   *  confirmed count and the body it is sent with always describe the same sheet. If the catalog
   *  moved underneath them the backend re-prices and refuses with 409 plan_changed. */
  function handleStart(confirmRequests: number, defer: boolean): void {
    start.mutate({ ...requestBody, confirm_requests: confirmRequests, defer_to_tomorrow: defer })
  }

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          resetPricing()
        }
        onOpenChange(next)
      }}
    >
      <SheetContent
        side="right"
        className="w-full gap-0 overflow-y-auto sm:max-w-xl"
      >
        <SheetHeader className="border-b">
          <SheetTitle>
            {remedyMode ? 'Underlying history' : 'Download ' + underlying.display_name}
          </SheetTitle>
          <SheetDescription>
            {remedyMode
              ? 'Bars for ' +
                underlying.fyers_symbol +
                ' itself, over the span of the selected expiries. An at the money band is centred ' +
                'on the close of the expiry day, so this is what it needs.'
              : pluralise(summary.expiries, 'expiry', 'expiries') +
                ' selected, ' +
                formatInteger(summary.contracts) +
                ' contracts discovered.'}
          </SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-5 p-4">
          {remedyMode ? (
            <div className="flex flex-col gap-2 rounded-md border px-3 py-2 text-xs text-muted-foreground">
              <span>
                This sheet downloads the underlying series only. It carries a placeholder expiry
                date of {SPOT_SHEET_PLACEHOLDER_DATE}, which is not in the catalog and is what
                keeps any contract out of the plan, so the preview below may warn that it is
                undiscovered. Nothing is downloaded for it.
              </span>
              <span className="tabular-nums">
                Range {requestBody.range_from} to {requestBody.range_to}, resolutions{' '}
                {requestBody.resolutions.join(', ')}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="self-start"
                onClick={leaveRemedy}
              >
                Back to the download sheet
              </Button>
            </div>
          ) : (
            <>
              <section className="flex flex-col gap-2">
                <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                  Resolutions
                </Label>
                <div className="flex flex-wrap gap-x-4 gap-y-2">
                  {choices.map((choice) => {
                    const checked = value.resolutions.includes(choice.code)
                    return (
                      <label
                        key={choice.code}
                        className="flex cursor-pointer items-center gap-2 text-sm"
                      >
                        <Checkbox
                          checked={checked}
                          onCheckedChange={(next) => {
                            toggleResolution(choice.code, next === true)
                          }}
                          aria-label={choice.label}
                        />
                        <span className="tabular-nums">{choice.label}</span>
                        {choice.isSeconds ? (
                          <Badge variant="outline" className="text-[0.65rem]">
                            last 30 trading days only
                          </Badge>
                        ) : null}
                      </label>
                    )
                  })}
                </div>
              </section>

              <section className="flex flex-wrap items-start gap-6">
                <div className="flex flex-col gap-2">
                  <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                    Instrument class
                  </Label>
                  <ToggleGroup
                    type="single"
                    value={value.instrumentClass}
                    onValueChange={(next) => {
                      if (next) {
                        update({ instrumentClass: next as InstrumentClass })
                      }
                    }}
                    variant="outline"
                    size="sm"
                  >
                    <ToggleGroupItem value="FUT">Futures</ToggleGroupItem>
                    <ToggleGroupItem value="OPT">Options</ToggleGroupItem>
                    <ToggleGroupItem value="BOTH">Both</ToggleGroupItem>
                  </ToggleGroup>
                </div>

                <div className="flex flex-col gap-2">
                  <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                    Option right
                  </Label>
                  <div className="flex items-center gap-4">
                    {(['CE', 'PE'] as const).map((right) => (
                      <label key={right} className="flex cursor-pointer items-center gap-2 text-sm">
                        <Checkbox
                          checked={value.optionTypes.includes(right)}
                          disabled={value.instrumentClass === 'FUT'}
                          onCheckedChange={(next) => {
                            toggleRight(right, next === true)
                          }}
                          aria-label={right === 'CE' ? 'Calls' : 'Puts'}
                        />
                        <span>{right === 'CE' ? 'Calls' : 'Puts'}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </section>

              <section className="flex flex-col gap-2">
                <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                  Strike scope
                </Label>
                <RadioGroup
                  value={value.strikeScope.mode}
                  onValueChange={(next) => {
                    setScopeMode(next as StrikeScopeValue['mode'])
                  }}
                  className="gap-2"
                >
                  <label className="flex cursor-pointer items-center gap-2 text-sm">
                    <RadioGroupItem value="all" />
                    <span>Every strike in the chain</span>
                  </label>

                  <div className="flex flex-col gap-1">
                    <label
                      className={cn(
                        'flex items-center gap-2 text-sm',
                        spot.available ? 'cursor-pointer' : 'cursor-not-allowed opacity-60',
                      )}
                    >
                      <RadioGroupItem value="atm_band" disabled={!spot.available} />
                      <span>At the money band</span>
                      {value.strikeScope.mode === 'atm_band' ? (
                        <>
                          <Input
                            type="number"
                            min={1}
                            max={200}
                            value={value.strikeScope.steps}
                            onChange={(event) => {
                              update({
                                strikeScope: {
                                  mode: 'atm_band',
                                  steps: Number(event.target.value),
                                },
                              })
                            }}
                            className="h-7 w-20 tabular-nums"
                            aria-label="Strike steps each side of the money"
                          />
                          <span className="text-xs text-muted-foreground">steps each side</span>
                        </>
                      ) : null}
                    </label>
                    {spot.reason ? (
                      <div className="flex flex-wrap items-center gap-2 pl-6">
                        <span className="text-xs text-destructive">{spot.reason}</span>
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          onClick={enterRemedy}
                        >
                          Download the underlying history
                        </Button>
                      </div>
                    ) : null}
                  </div>

                  <div className="flex flex-col gap-1">
                    <label className="flex cursor-pointer items-center gap-2 text-sm">
                      <RadioGroupItem value="explicit" />
                      <span>Named strikes</span>
                    </label>
                    {value.strikeScope.mode === 'explicit' ? (
                      <Input
                        value={strikeText}
                        placeholder="23000 23100 23200"
                        onChange={(event) => {
                          setStrikeText(event.target.value)
                          update({
                            strikeScope: {
                              mode: 'explicit',
                              strikes: parseStrikes(event.target.value),
                            },
                          })
                        }}
                        className="ml-6 h-7 max-w-md"
                        aria-label="Strikes to download"
                      />
                    ) : null}
                  </div>
                </RadioGroup>
              </section>

              <section className="flex flex-wrap gap-6">
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Switch
                    checked={value.includeOi}
                    onCheckedChange={(next) => {
                      update({ includeOi: next })
                    }}
                    aria-label="Include open interest"
                  />
                  <span>Open interest</span>
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Switch
                    checked={value.forceRefresh}
                    onCheckedChange={(next) => {
                      update({ forceRefresh: next })
                    }}
                    aria-label="Re-download windows already held"
                  />
                  <span>Re-download what is already held</span>
                </label>
              </section>
            </>
          )}

          {problems.length > 0 ? (
            <ul className="flex flex-col gap-1 text-xs text-destructive">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          ) : null}

          <PlanPreviewCard
            preview={currentPreview}
            isPricing={price.isPending}
            failure={failure}
            remedyAction={remedyAction}
            deferToTomorrow={deferToTomorrow}
            onDeferChange={setDeferToTomorrow}
            onStart={handleStart}
            isStarting={start.isPending}
          />
        </div>
      </SheetContent>
    </Sheet>
  )
}

export default DownloadSheet
