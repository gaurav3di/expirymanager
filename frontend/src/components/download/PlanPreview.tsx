import type { ReactNode } from 'react'
import { cn } from 'cn'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api/client'
import {
  formatBytes,
  formatCompact,
  formatDuration,
  formatInteger,
  humaniseCode,
} from '@/lib/format'

// The estimate that gates Start.
//
// This card is the reason the product can be trusted with a hundred thousand request a day
// budget. Everything on it is the planner's own arithmetic, returned by POST /downloads/plan,
// which costs zero Fyers requests. Nothing here recomputes a cost, a row count or a budget line:
// a second opinion on the price is exactly how a user ends up agreeing to a number the pipeline
// never intended to spend.
//
// The gate has three parts:
//
//  1. Start is disabled with an INLINE REASON when the plan does not fit in what is left of
//     today's budget. Not a toast, not a refusal after the click.
//  2. Queue for tomorrow is offered beside that reason, because a plan that does not fit today
//     is still a plan the user wants; the backend creates it in deferred_budget and the sweep
//     picks it up when the IST date rolls.
//  3. `requests_estimated` is carried back on submit as `confirm_requests`. If the catalog moved
//     underneath the preview the commit is refused with 409 plan_changed and the user re-reads a
//     price rather than starting work nobody priced.

// ---------------------------------------------------------------------------
// The wire shape
// ---------------------------------------------------------------------------

/** One machine readable warning from the planner, so a remedy can be rendered rather than a
 *  sentence guessed at from a message. */
export interface PlanWarningWire {
  code: string
  message: string
  detail: unknown
}

/** `PlanPreview` from api/schemas/downloads.py.
 *
 *  Not the `PlanPreview` in lib/api/types.ts, which was written against the API.md sketch and
 *  names fields (contracts_matched, chunks_total, minutes_estimated, budget_remaining) the
 *  backend does not send. Mirroring the real model here is what makes a rename upstream a
 *  compile error instead of a card full of "not set". */
export interface PlanPreviewBody {
  tasks_total: number
  requests_estimated: number
  chunks_skipped_covered: number
  contracts_sealed_skipped: number
  rows_estimated: number
  bytes_estimated: number
  eta_seconds: number
  budget_used_today: number
  budget_remaining_today: number
  budget_after: number
  warnings: string[]

  expiries_planned: number
  contracts_planned: number
  discovery_tasks: number
  estimated_downstream_requests: number
  spot_tasks: number
  budget_allowance: number
  reserve_applied: boolean
  exceeds_budget: boolean
  warning_details: PlanWarningWire[]
}

/** The 202 body of POST /downloads. */
export interface DownloadAcceptedBody {
  job_id: string
  status: string
  total_tasks: number
  est_requests: number
  deferred: boolean
  preview: PlanPreviewBody | null
}

// ---------------------------------------------------------------------------
// Failures the sheet has to explain rather than report
// ---------------------------------------------------------------------------

export type PlanFailureRemedy = 'discover_contracts' | 'spot_history' | 'reconnect' | null

export interface PlanFailure {
  code: string
  title: string
  message: string
  /** What the screen can offer to do about it with one click. */
  remedy: PlanFailureRemedy
  /** True when re-pricing the same sheet is the next step, which is what a stale preview needs. */
  repriceable: boolean
}

/**
 * Turns a plan or commit error into something with a next step attached.
 *
 * The four codes named here are the ones the planner and the job service raise for a sheet that
 * is wrong rather than for a backend that is broken, and each has a different remedy. Anything
 * else keeps the backend's own sentence, which is written to be shown.
 */
export function describePlanFailure(error: unknown): PlanFailure {
  if (!(error instanceof ApiError)) {
    return {
      code: 'unknown',
      title: 'The plan could not be priced',
      message: error instanceof Error ? error.message : String(error),
      remedy: null,
      repriceable: true,
    }
  }

  switch (error.code) {
    case 'no_contracts_discovered':
      return {
        code: error.code,
        title: 'The contracts are not discovered yet',
        message: error.message,
        remedy: 'discover_contracts',
        repriceable: false,
      }
    case 'strike_scope_needs_spot':
      return {
        code: error.code,
        title: 'An at the money band needs the underlying history',
        message: error.message,
        remedy: 'spot_history',
        repriceable: false,
      }
    case 'needs_reauth':
      return {
        code: error.code,
        title: 'The Fyers session has to be renewed',
        message: error.message,
        remedy: 'reconnect',
        repriceable: false,
      }
    case 'plan_changed':
      return {
        code: error.code,
        title: 'The estimate is out of date',
        message:
          error.message +
          ' The catalog changed between the price and the start, so the sheet has been priced ' +
          'again. Read the new estimate before starting it.',
        remedy: null,
        repriceable: true,
      }
    case 'nothing_to_download':
      return {
        code: error.code,
        title: 'Nothing to download',
        message: error.message,
        remedy: null,
        repriceable: true,
      }
    default:
      return {
        code: error.code,
        title: error.isRateLimited ? 'Too many requests to this app' : 'The plan was refused',
        message: error.message,
        remedy: error.isNeedsReauth ? 'reconnect' : null,
        repriceable: true,
      }
  }
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

export interface StartDecision {
  canStart: boolean
  /** Rendered next to the disabled button. Null when Start is live. */
  reason: string | null
  /** True when queueing for tomorrow is the way past the reason. */
  offerDefer: boolean
  /** The number to send as confirm_requests. Exactly what was shown, never recomputed. */
  confirmRequests: number
}

/**
 * Whether this preview may be started, and if not, the sentence that says why.
 *
 * The budget comparison is the planner's own: `exceeds_budget` is computed against
 * `budget_allowance`, which is the day's remainder for a user sheet and the sweep reserve for a
 * scheduled one. Recomputing it here from remaining and estimated would disagree with the commit
 * gate the moment a reserve applies.
 */
export function startDecision(
  preview: PlanPreviewBody | null,
  options: { deferToTomorrow?: boolean } = {},
): StartDecision {
  if (!preview) {
    return {
      canStart: false,
      reason: 'Price this sheet before starting it.',
      offerDefer: false,
      confirmRequests: 0,
    }
  }

  const confirmRequests = preview.requests_estimated

  if (preview.tasks_total === 0) {
    return {
      canStart: false,
      reason:
        'This sheet asks for nothing new. Every window it covers is already recorded in the ' +
        'coverage ledger. Tick more expiries, add a resolution, or force a re-download.',
      offerDefer: false,
      confirmRequests,
    }
  }

  if (preview.exceeds_budget && !options.deferToTomorrow) {
    return {
      canStart: false,
      reason:
        'This plan needs ' +
        formatInteger(preview.requests_estimated) +
        ' requests and ' +
        formatInteger(preview.budget_allowance) +
        (preview.reserve_applied
          ? ' remain inside the sweep reserve today.'
          : " remain in today's budget.") +
        ' Queue it for tomorrow, or narrow the selection.',
      offerDefer: true,
      confirmRequests,
    }
  }

  return { canStart: true, reason: null, offerDefer: preview.exceeds_budget, confirmRequests }
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

interface FigureProps {
  term: string
  value: string
  hint?: string
  tone?: 'default' | 'negative'
}

function Figure({ term, value, hint, tone = 'default' }: FigureProps) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-md border px-3 py-2">
      <span className="text-[0.65rem] uppercase tracking-wider text-muted-foreground">{term}</span>
      <span
        className={cn(
          'text-sm font-medium tabular-nums',
          tone === 'negative' && 'text-destructive',
        )}
      >
        {value}
      </span>
      {hint ? <span className="text-[0.65rem] text-muted-foreground">{hint}</span> : null}
    </div>
  )
}

export interface PlanPreviewCardProps {
  preview: PlanPreviewBody | null
  isPricing: boolean
  failure: PlanFailure | null
  /** Rendered under the failure when it carries a remedy the screen can act on. */
  remedyAction?: ReactNode
  deferToTomorrow: boolean
  onDeferChange: (defer: boolean) => void
  onStart: (confirmRequests: number, deferToTomorrow: boolean) => void
  isStarting: boolean
  className?: string
}

export function PlanPreviewCard({
  preview,
  isPricing,
  failure,
  remedyAction,
  deferToTomorrow,
  onDeferChange,
  onStart,
  isStarting,
  className,
}: PlanPreviewCardProps) {
  const decision = startDecision(preview, { deferToTomorrow })

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {failure ? (
        <Alert variant="destructive">
          <AlertTitle>{failure.title}</AlertTitle>
          <AlertDescription>
            <span>{failure.message}</span>
            {remedyAction ? <div className="pt-2">{remedyAction}</div> : null}
          </AlertDescription>
        </Alert>
      ) : null}

      {isPricing ? (
        <div className="flex items-center gap-2 rounded-md border px-3 py-6 text-sm text-muted-foreground">
          <Spinner className="size-4" />
          Pricing this sheet from local state. This costs no Fyers requests.
        </div>
      ) : null}

      {preview && !isPricing ? (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Figure
              term="Requests"
              value={formatInteger(preview.requests_estimated)}
              hint={
                preview.discovery_tasks > 0
                  ? formatInteger(preview.discovery_tasks) +
                    ' discovery, ' +
                    formatInteger(preview.estimated_downstream_requests) +
                    ' estimated behind them'
                  : formatInteger(preview.tasks_total) + ' tasks'
              }
            />
            <Figure
              term="Rows"
              value={formatCompact(preview.rows_estimated)}
              hint={formatInteger(preview.contracts_planned) + ' contracts'}
            />
            <Figure
              term="On disk"
              value={formatBytes(preview.bytes_estimated)}
              hint="estimated, before compression"
            />
            <Figure
              term="Wall clock"
              value={formatDuration(preview.eta_seconds)}
              hint="at the governor's pace"
            />
            <Figure
              term="Budget left today"
              value={formatInteger(preview.budget_remaining_today)}
              hint={formatInteger(preview.budget_used_today) + ' already used'}
            />
            <Figure
              term="Budget after"
              value={formatInteger(preview.budget_after)}
              tone={preview.budget_after < 0 ? 'negative' : 'default'}
              hint={
                preview.reserve_applied
                  ? formatInteger(preview.budget_allowance) + ' allowed inside the sweep reserve'
                  : undefined
              }
            />
          </div>

          <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <div className="flex items-baseline gap-1.5">
              <dt>Expiries priced</dt>
              <dd className="tabular-nums text-foreground">
                {formatInteger(preview.expiries_planned)}
              </dd>
            </div>
            <div className="flex items-baseline gap-1.5">
              <dt>Already held, skipped</dt>
              <dd className="tabular-nums text-foreground">
                {formatInteger(preview.chunks_skipped_covered)} chunks
              </dd>
            </div>
            <div className="flex items-baseline gap-1.5">
              <dt>Sealed, skipped</dt>
              <dd className="tabular-nums text-foreground">
                {formatInteger(preview.contracts_sealed_skipped)} contracts
              </dd>
            </div>
            {preview.spot_tasks > 0 ? (
              <div className="flex items-baseline gap-1.5">
                <dt>Underlying history</dt>
                <dd className="tabular-nums text-foreground">
                  {formatInteger(preview.spot_tasks)} chunks
                </dd>
              </div>
            ) : null}
          </dl>

          {preview.warning_details.length > 0 ? (
            <ul className="flex flex-col gap-1.5">
              {preview.warning_details.map((warning) => (
                <li
                  key={warning.code + warning.message}
                  className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
                >
                  <Badge variant="outline" className="shrink-0 text-[0.65rem]">
                    {humaniseCode(warning.code)}
                  </Badge>
                  <span className="text-muted-foreground">{warning.message}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 border-t pt-3">
        {decision.reason ? (
          <p
            className={cn(
              'min-w-0 flex-1 text-xs',
              preview?.exceeds_budget ? 'text-destructive' : 'text-muted-foreground',
            )}
          >
            {decision.reason}
          </p>
        ) : (
          <p className="min-w-0 flex-1 text-xs text-muted-foreground">
            {preview
              ? formatInteger(decision.confirmRequests) +
                ' requests will be confirmed exactly as priced.'
              : ''}
          </p>
        )}

        {decision.offerDefer ? (
          <Button
            type="button"
            variant={deferToTomorrow ? 'default' : 'outline'}
            size="sm"
            onClick={() => {
              onDeferChange(!deferToTomorrow)
            }}
          >
            {deferToTomorrow ? 'Queued for tomorrow' : 'Queue for tomorrow'}
          </Button>
        ) : null}

        <Button
          type="button"
          size="sm"
          disabled={!decision.canStart || isStarting}
          onClick={() => {
            onStart(decision.confirmRequests, deferToTomorrow)
          }}
        >
          {isStarting ? 'Starting' : deferToTomorrow ? 'Queue download' : 'Start download'}
        </Button>
      </div>
    </div>
  )
}

export default PlanPreviewCard
