/**
 * The export builder.
 *
 * INCOMPLETE. The three pure functions below are the real ones, written against the spec in
 * `src/routes/exports.test.ts` and passing it. The dialog body is a placeholder: it explains
 * itself and offers no way to start an export.
 *
 * Why this file was missing rather than merely unfinished: the repository's root `.gitignore`
 * carried a bare `exports/`, meant for the runtime data directory, which also matched
 * `frontend/src/components/exports`. The original component was written and git ignored it
 * silently, so it never reached a commit and is not in the history. The pattern is now anchored
 * to `/exports/`, so this directory is tracked.
 *
 * What is left to build, all of it inside `ExportDialog` itself:
 *
 *   - the underlying picker, the expiry window, the kind and right, and the file decisions
 *     (format, layout, compression, denormalise, include catalog), as a form over `ExportForm`
 *   - the resolution pills, which must be fetched from the coverage grid endpoint for the chosen
 *     underlying and rendered from what `resolutionOptionsFrom` returns for that response, never
 *     from a hardcoded list: the ledger is the authority on what was actually fetched, and a
 *     resolution that was fetched and came back empty has to be shown and refused with the
 *     reason rather than hidden
 *   - a running scope estimate from `estimateScope`
 *   - the create mutation, posting exactly what `buildExportBody` returns for the form and
 *     nothing assembled a second time
 *
 * Two assertions in `exports.test.ts` read this file's source and will fail until that work is
 * done. They are the accurate signal that the dialog is still a stub. Satisfying them by writing
 * the strings they search for, even inside a comment like this one, would only teach the suite
 * to lie.
 */

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type {
  ExportCreateRequest,
  ExportFormat,
  ExportLayout,
  ExportScope,
  OptionType,
  Underlying,
} from '@/lib/api/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** One row of the coverage ledger: one expiry against one resolution. */
export interface CoverageCell {
  expiry_date: string
  res_id: number
  contracts_total: number
  contracts_with_data: number
  contracts_missing: number
  chunks_ok: number
  chunks_empty: number
  chunks_error: number
  rows: number
}

/** A resolution the ledger names as a column, with the Fyers code that goes on the wire. */
export interface CoverageResolution {
  res_id: number
  fyers_code: string
  label: string
  seconds: number
}

/** `GET /coverage/grid` for one underlying. */
export interface CoverageGrid {
  underlying_id: number
  resolutions: CoverageResolution[]
  cells: CoverageCell[]
}

/**
 * One resolution pill, rolled up over every expiry in the grid.
 *
 * `exportable` is false when the resolution holds no rows. The chunk counts survive that so the
 * screen can say fetched-and-empty rather than never-fetched, which are different answers: an
 * expired contract that Fyers has nothing for is a final answer, not a gap to retry.
 */
export interface ResolutionOption extends CoverageResolution {
  rows: number
  chunks_ok: number
  chunks_empty: number
  chunks_error: number
  exportable: boolean
}

/** Only the fields the export screen reads off an underlying. */
export type UnderlyingOption = Pick<Underlying, 'underlying_id' | 'display_name'>

/** The dialog's form state, before it becomes a request body. */
export interface ExportForm {
  underlyingId: number
  /** Fyers codes such as "1" and "D". Never res_ids: see `buildExportBody`. */
  resolutionCodes: string[]
  expiryFrom: string
  expiryTo: string
  kind: ExportScope['kind']
  optionType: OptionType | ''
  format: ExportFormat
  layout: ExportLayout
  compression: string
  denormalise: boolean
  includeCatalog: boolean
}

/** `ExportScope` plus the right, which the API accepts and `types.ts` does not yet name. */
export interface ExportScopeBody extends ExportScope {
  option_type: OptionType | null
}

export interface ExportCreateBody extends Omit<ExportCreateRequest, 'scope'> {
  scope: ExportScopeBody
}

/** What `estimateScope` can say about a scope before anything is sent. */
export interface ScopeEstimate {
  rows: number
  expiries: number
  resolutions: number
}

/** The part of the form that narrows the scope. */
export interface ScopeSelection {
  resolutionCodes: string[]
  expiryFrom: string
  expiryTo: string
}

// ---------------------------------------------------------------------------
// The pure functions the screen and the tests share
// ---------------------------------------------------------------------------

/**
 * The resolution pills, derived from the coverage ledger.
 *
 * Only resolutions the grid names are offered. Nothing is invented from the fourteen the broker
 * serves or from what the underlying is configured to fetch, because neither of those is
 * evidence that anything was downloaded.
 */
export function resolutionOptionsFrom(grid: CoverageGrid | undefined): ResolutionOption[] {
  if (!grid) {
    return []
  }
  const byRes = new Map<number, ResolutionOption>()
  for (const resolution of grid.resolutions) {
    byRes.set(resolution.res_id, {
      ...resolution,
      rows: 0,
      chunks_ok: 0,
      chunks_empty: 0,
      chunks_error: 0,
      exportable: false,
    })
  }
  for (const cell of grid.cells) {
    const option = byRes.get(cell.res_id)
    if (!option) {
      // A cell for a resolution the grid did not name as a column. There is no pill to put it
      // on, so it is skipped here; `summariseGrid` on the dashboard still counts its rows.
      continue
    }
    option.rows += cell.rows
    option.chunks_ok += cell.chunks_ok
    option.chunks_empty += cell.chunks_empty
    option.chunks_error += cell.chunks_error
  }
  for (const option of byRes.values()) {
    option.exportable = option.rows > 0
  }
  return [...byRes.values()]
}

/** True when `date` falls inside the window. An empty bound is not a bound. */
function withinWindow(date: string, from: string, to: string): boolean {
  if (from && date < from) {
    return false
  }
  if (to && date > to) {
    return false
  }
  return true
}

/**
 * What the chosen scope would produce, counted from the ledger rather than guessed.
 *
 * An empty `resolutionCodes` means every resolution held, which is what the backend does with an
 * empty list. `resolutions` counts only those that would actually contribute rows: a resolution
 * that was fetched and came back empty produces no file and is not one the export would produce.
 */
export function estimateScope(
  grid: CoverageGrid | undefined,
  options: ResolutionOption[],
  selection: ScopeSelection,
): ScopeEstimate {
  if (!grid) {
    return { rows: 0, expiries: 0, resolutions: 0 }
  }
  const chosen = new Set(
    options
      .filter(
        (option) =>
          selection.resolutionCodes.length === 0 ||
          selection.resolutionCodes.includes(option.fyers_code),
      )
      .map((option) => option.res_id),
  )

  let rows = 0
  const expiries = new Set<string>()
  const resolutions = new Set<number>()
  for (const cell of grid.cells) {
    if (!chosen.has(cell.res_id)) {
      continue
    }
    if (!withinWindow(cell.expiry_date, selection.expiryFrom, selection.expiryTo)) {
      continue
    }
    rows += cell.rows
    if (cell.rows > 0) {
      expiries.add(cell.expiry_date)
      resolutions.add(cell.res_id)
    }
  }
  return { rows, expiries: expiries.size, resolutions: resolutions.size }
}

/**
 * The request body for `POST /exports`.
 *
 * Resolutions go on the wire as Fyers codes. res_id 1 is the five second series while the code
 * "1" is one minute, so sending an id where a code belongs exports a different series entirely
 * and nothing anywhere reports an error. An empty expiry box is null rather than an empty
 * string, because the backend reads null as no bound and "" as a date it cannot parse. The right
 * is sent only when the scope is options.
 */
export function buildExportBody(form: ExportForm): ExportCreateBody {
  return {
    format: form.format,
    layout: form.layout,
    compression: form.compression,
    denormalise: form.denormalise,
    scope: {
      underlying_id: form.underlyingId,
      resolutions: [...form.resolutionCodes],
      expiry_from: form.expiryFrom || null,
      expiry_to: form.expiryTo || null,
      kind: form.kind,
      option_type: form.kind === 'OPT' && form.optionType ? form.optionType : null,
      include_catalog: form.includeCatalog,
    },
  }
}

// ---------------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------------

export interface ExportDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  underlyings: UnderlyingOption[]
}

/**
 * Placeholder. See the note at the top of this file for what belongs here.
 *
 * It says so on its face rather than rendering a form that cannot submit, because a builder that
 * looks finished and quietly does nothing is worse than one that admits what it is.
 */
export function ExportDialog({ open, onOpenChange, underlyings }: ExportDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Export builder not implemented</DialogTitle>
          <DialogDescription>
            This dialog is a placeholder. The rest of the export screen works: finished exports
            are listed below, and each one can be downloaded or deleted.
          </DialogDescription>
        </DialogHeader>
        <div className="text-muted-foreground space-y-2 text-sm">
          <p>
            Building a new export needs the resolution pills, which are read from the coverage
            ledger for the chosen underlying so that only what was actually downloaded can be
            asked for. That part of the component was lost before it was ever committed.
          </p>
          <p>
            {underlyings.length === 0
              ? 'No underlyings are registered yet, so there would be nothing to export from.'
              : `${underlyings.length} underlying${underlyings.length === 1 ? '' : 's'} would be available to export from.`}
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
