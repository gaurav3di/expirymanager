import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { apiErrorMessage } from '@/components/settings/BrokerPanel'
import { Button } from '@/components/ui/button'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import { formatDateTime, humaniseCode } from '@/lib/format'
import { describeCron } from '@/routes/schedules'
import type { ScheduleRow } from '@/routes/schedules'

// Create or edit one schedule.
//
// Two things here are deliberate.
//
// The kind list is not written down in this file. It is the set of kinds the backend already has
// schedules for, passed in from the list screen. A hand kept list of kinds would drift from
// scheduler/jobs_def.py and offer a kind with no registered action, which is a row that fires
// forever and does nothing. Kind is also create-only, because the backend's own update model has
// no kind field: re-kinding a schedule would keep a run history that is a record of something
// else.
//
// The cron preview is the same describeCron the table uses, so what the user reads before saving
// is exactly what the row will read afterwards. When the expression is one this app cannot put
// into words, the preview says so instead of inventing a sentence. The scheduler validates the
// expression for real on save, with APScheduler's own parser, and its 400 lands under the field.

const DEFAULT_TIMEZONE = 'Asia/Kolkata'
const DEFAULT_CRON = '0 18 * * 1-5'
const DEFAULT_GRACE_SECONDS = 3600

export interface ScheduleDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null creates a new schedule. A row edits that row. */
  schedule: ScheduleRow | null
  /** The kinds that exist on this backend, from the schedules the list screen already read. */
  kinds: string[]
}

export interface FormState {
  name: string
  kind: string
  cron: string
  timezone: string
  enabled: boolean
  trading_days_only: boolean
  misfire_grace_seconds: string
  max_requests_per_run: string
  params: string
}

function initialState(schedule: ScheduleRow | null, kinds: string[]): FormState {
  if (schedule) {
    return {
      name: schedule.name,
      kind: schedule.kind,
      cron: schedule.cron,
      timezone: schedule.timezone,
      enabled: schedule.enabled,
      trading_days_only: schedule.trading_days_only,
      misfire_grace_seconds: String(schedule.misfire_grace_seconds),
      max_requests_per_run:
        schedule.max_requests_per_run === null ? '' : String(schedule.max_requests_per_run),
      params: JSON.stringify(schedule.params ?? {}, null, 2),
    }
  }
  return {
    name: '',
    kind: kinds[0] ?? '',
    cron: DEFAULT_CRON,
    timezone: DEFAULT_TIMEZONE,
    enabled: true,
    trading_days_only: true,
    misfire_grace_seconds: String(DEFAULT_GRACE_SECONDS),
    max_requests_per_run: '',
    params: '{}',
  }
}

/** The preview line under the cron field. Separated from the component so the wording is one
 *  string and not three branches inside JSX. */
export function cronPreview(expression: string, timezone: string): string {
  const described = describeCron(expression, timezone)
  if (described === expression || described.trim() === '') {
    return (
      'This app cannot put that expression into words. The scheduler checks it with its own ' +
      'parser when you save, and says so if it cannot run it.'
    )
  }
  return described
}

export interface ScheduleWriteBody {
  name: string
  kind?: string
  cron: string
  timezone: string
  enabled: boolean
  trading_days_only: boolean
  misfire_grace_seconds: number
  max_requests_per_run: number | null
  params: Record<string, unknown>
}

/**
 * Turns the form into the body the backend takes, or reports why it cannot.
 *
 * Exported so the shape that crosses the wire is asserted directly rather than inferred from a
 * rendered form: every field the scheduler needs, with the empty max-requests box becoming null
 * rather than 0, which would be a schedule allowed no requests at all.
 */
export function buildScheduleBody(
  form: FormState,
  mode: 'create' | 'edit',
): { body: ScheduleWriteBody } | { error: string } {
  const name = form.name.trim()
  if (name === '') {
    return { error: 'A schedule needs a name.' }
  }
  if (mode === 'create' && form.kind.trim() === '') {
    return { error: 'Pick what this schedule should do.' }
  }
  if (form.cron.trim().split(/\s+/).length !== 5) {
    return { error: 'A cron expression has five fields: minute hour day month day_of_week.' }
  }

  const grace = Number(form.misfire_grace_seconds)
  if (!Number.isFinite(grace) || grace < 0 || grace > 86400) {
    return { error: 'The misfire grace is a number of seconds between 0 and 86400.' }
  }

  let maxRequests: number | null = null
  if (form.max_requests_per_run.trim() !== '') {
    const parsed = Number(form.max_requests_per_run)
    if (!Number.isInteger(parsed) || parsed < 1) {
      return { error: 'A request ceiling is a whole number of at least 1, or leave it empty.' }
    }
    maxRequests = parsed
  }

  let params: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(form.params.trim() === '' ? '{}' : form.params)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { error: 'Parameters must be a JSON object, for example {"max_expiries": 40}.' }
    }
    params = parsed as Record<string, unknown>
  } catch {
    return { error: 'Parameters are not valid JSON.' }
  }

  const body: ScheduleWriteBody = {
    name,
    cron: form.cron.trim().split(/\s+/).join(' '),
    timezone: form.timezone.trim() === '' ? DEFAULT_TIMEZONE : form.timezone.trim(),
    enabled: form.enabled,
    trading_days_only: form.trading_days_only,
    misfire_grace_seconds: Math.round(grace),
    max_requests_per_run: maxRequests,
    params,
  }
  // The update model has no kind field at all, so sending one would be a 422 on an otherwise
  // valid edit.
  if (mode === 'create') {
    body.kind = form.kind
  }
  return { body }
}

export function ScheduleDialog({ open, onOpenChange, schedule, kinds }: ScheduleDialogProps) {
  const client = useQueryClient()
  const mode: 'create' | 'edit' = schedule ? 'edit' : 'create'
  const [form, setForm] = useState<FormState>(() => initialState(schedule, kinds))
  const [localError, setLocalError] = useState<string | null>(null)

  // Reopening on a different row must not show the previous row's values. The dialog is mounted
  // permanently by the screen, so this is the only reset there is.
  useEffect(() => {
    if (open) {
      setForm(initialState(schedule, kinds))
      setLocalError(null)
    }
    // kinds is a fresh array on every render of the parent; the id is what actually identifies
    // the row being edited.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [open, schedule?.schedule_id])

  const preview = useMemo(() => cronPreview(form.cron, form.timezone), [form.cron, form.timezone])

  const save = useMutation({
    mutationFn: (body: ScheduleWriteBody) =>
      schedule
        ? api.patch<ScheduleRow>('/schedules/' + schedule.schedule_id, { body })
        : api.post<ScheduleRow>('/schedules', { body }),
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all() })
      toast.success(
        row.name +
          ' saved. ' +
          (row.enabled
            ? row.next_fire_at
              ? 'Next fire ' + formatDateTime(row.next_fire_at) + '.'
              : 'It is on, and the scheduler reported no next fire for it.'
            : 'It is switched off, so the timer will not reach it.'),
      )
      onOpenChange(false)
    },
  })

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }))
  }

  const submit = () => {
    const built = buildScheduleBody(form, mode)
    if ('error' in built) {
      setLocalError(built.error)
      return
    }
    setLocalError(null)
    save.mutate(built.body)
  }

  const serverError = apiErrorMessage(save.error)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{schedule ? 'Edit ' + schedule.name : 'New schedule'}</DialogTitle>
          <DialogDescription>
            {schedule?.is_builtin
              ? 'A built in schedule can be re-timed, re-parameterised and switched off. What it does is fixed, because the code that does it is what defines it.'
              : 'A schedule fires on a cron expression in one timezone and queues the same work the built in schedules queue.'}
          </DialogDescription>
        </DialogHeader>

        <div className="flex max-h-[60vh] flex-col gap-3 overflow-y-auto pr-1">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="schedule-name">Name</Label>
            <Input
              id="schedule-name"
              value={form.name}
              onChange={(event) => set('name', event.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="schedule-kind">Does</Label>
            {mode === 'edit' ? (
              <p className="font-mono text-xs text-muted-foreground">
                {form.kind}. A different job is a different schedule, so this cannot be changed
                here.
              </p>
            ) : (
              <Select value={form.kind} onValueChange={(next) => set('kind', next)}>
                <SelectTrigger id="schedule-kind" className="w-full">
                  <SelectValue placeholder="Pick a job" />
                </SelectTrigger>
                <SelectContent>
                  {kinds.map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {humaniseCode(kind)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="schedule-cron">Cron expression</Label>
            <Input
              id="schedule-cron"
              className="font-mono"
              value={form.cron}
              onChange={(event) => set('cron', event.target.value)}
              placeholder="minute hour day month day_of_week"
            />
            <p className="text-xs text-muted-foreground">{preview}</p>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="schedule-timezone">Timezone</Label>
            <Input
              id="schedule-timezone"
              value={form.timezone}
              onChange={(event) => set('timezone', event.target.value)}
            />
          </div>

          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 border-t pt-3">
            <div className="max-w-prose">
              <p className="text-sm">Trading days only</p>
              <p className="text-xs text-muted-foreground">
                Skip a fire on a day the exchange did not trade. Observed days decide this, so a
                special Saturday session counts as a trading day and a weekday the exchange was
                shut does not.
              </p>
            </div>
            <Switch
              checked={form.trading_days_only}
              aria-label="Trading days only"
              onCheckedChange={(next) => set('trading_days_only', next)}
            />
          </div>

          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 border-t pt-3">
            <div className="max-w-prose">
              <p className="text-sm">On</p>
              <p className="text-xs text-muted-foreground">
                Off keeps the row and its history and stops the timer reaching it.
              </p>
            </div>
            <Switch
              checked={form.enabled}
              aria-label="Enabled"
              onCheckedChange={(next) => set('enabled', next)}
            />
          </div>

          <div className="grid gap-3 border-t pt-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="schedule-grace">Misfire grace, seconds</Label>
              <Input
                id="schedule-grace"
                type="number"
                min={0}
                max={86400}
                value={form.misfire_grace_seconds}
                onChange={(event) => set('misfire_grace_seconds', event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                How late a fire may still run after a sleeping laptop wakes up. Past this, the
                fire is dropped rather than run for a day that is over.
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="schedule-max-requests">Requests a run</Label>
              <Input
                id="schedule-max-requests"
                type="number"
                min={1}
                value={form.max_requests_per_run}
                placeholder="no ceiling"
                onChange={(event) => set('max_requests_per_run', event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                A ceiling on what one fire may spend from the daily Fyers budget. Empty means no
                ceiling of its own.
              </p>
            </div>
          </div>

          <div className="flex flex-col gap-1.5 border-t pt-3">
            <Label htmlFor="schedule-params">Parameters</Label>
            <Textarea
              id="schedule-params"
              className="h-28 font-mono text-xs"
              value={form.params}
              onChange={(event) => set('params', event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              A JSON object, read by the job itself. An empty underlying_ids list means every
              registered underlying.
            </p>
          </div>

          {localError ? <p className="text-sm text-destructive">{localError}</p> : null}
          {serverError ? <p className="text-sm text-destructive">{serverError}</p> : null}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" onClick={submit} disabled={save.isPending}>
            {save.isPending ? 'Saving' : schedule ? 'Save changes' : 'Create schedule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default ScheduleDialog
