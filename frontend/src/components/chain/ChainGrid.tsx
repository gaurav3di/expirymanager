import { useMemo } from 'react'
import { cn } from 'cn'

import { EmptyState } from '@/components/common/EmptyState'
import { formatCompact, formatInteger, formatPrice, formatStrike } from '@/lib/format'

// The option chain grid.
//
// Calls on the left, puts on the right, strikes down the middle, which is the layout every option
// trader already reads without being taught. The at the money row is highlighted from the row's
// own is_atm flag and never by comparing strike with atm_strike: both cross the wire as JSON
// doubles, and a float equality test that misses highlights the wrong row silently.
//
// Leg cells are dimmed when the leg exists but holds no bar at this instant, which is a different
// fact from the leg not existing at all. A strike whose put was never listed shows an empty cell;
// a strike whose put exists but did not trade in this minute shows its symbol greyed with no
// numbers. Collapsing the two would make a thin strike look like a catalogue gap.

export interface ChainLegValues {
  contract_id: number
  fyers_symbol: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  oi: number | null
}

export interface ChainGridRow {
  strike: number
  lot_size: number | null
  is_atm: boolean
  ce: ChainLegValues | null
  pe: ChainLegValues | null
}

export interface ChainGridProps {
  rows: readonly ChainGridRow[]
  spot: number | null
  atmStrike: number | null
  isLoading?: boolean
  /** Opens the chart for one leg. */
  onOpenLeg?: (leg: ChainLegValues) => void
  className?: string
}

function hasQuote(leg: ChainLegValues | null): leg is ChainLegValues {
  return leg !== null && leg.close !== null
}

function LegCells({
  leg,
  onOpen,
}: {
  leg: ChainLegValues | null
  onOpen?: (leg: ChainLegValues) => void
}) {
  if (leg === null) {
    return (
      <>
        <td className="px-2 py-1 text-right text-xs text-muted-foreground">not listed</td>
        <td className="px-2 py-1" />
        <td className="px-2 py-1" />
      </>
    )
  }

  // A leg with no bar at this instant is a different fact from a leg that does not exist, so it
  // keeps its symbol and loses its numbers rather than disappearing.
  const quoted = hasQuote(leg)
  return (
    <>
      <td className="px-2 py-1 text-right text-sm font-medium tabular-nums">
        {onOpen === undefined ? (
          <span className={cn(!quoted && 'text-muted-foreground')} title={leg.fyers_symbol}>
            {quoted ? formatPrice(leg.close) : 'no bar'}
          </span>
        ) : (
          <button
            type="button"
            className={cn(
              'rounded-sm outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50',
              !quoted && 'text-muted-foreground',
            )}
            title={leg.fyers_symbol}
            onClick={() => onOpen(leg)}
          >
            {quoted ? formatPrice(leg.close) : 'no bar'}
          </button>
        )}
      </td>
      <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
        {leg.volume === null ? '' : formatCompact(leg.volume)}
      </td>
      <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
        {leg.oi === null ? '' : formatCompact(leg.oi)}
      </td>
    </>
  )
}

/** Totals the grid footer reports. Pure and exported: the put call ratio is the one number on
 *  this screen a reader will quote elsewhere, so it is worth pinning. */
export function chainTotals(rows: readonly ChainGridRow[]): {
  callOi: number
  putOi: number
  callVolume: number
  putVolume: number
  pcr: number | null
} {
  let callOi = 0
  let putOi = 0
  let callVolume = 0
  let putVolume = 0
  for (const row of rows) {
    callOi += row.ce?.oi ?? 0
    putOi += row.pe?.oi ?? 0
    callVolume += row.ce?.volume ?? 0
    putVolume += row.pe?.volume ?? 0
  }
  return {
    callOi,
    putOi,
    callVolume,
    putVolume,
    // Null rather than Infinity or zero. A chain with no call open interest has no ratio, and
    // printing 0.00 there would read as a real measurement.
    pcr: callOi > 0 ? putOi / callOi : null,
  }
}

export function ChainGrid({
  rows,
  spot,
  atmStrike,
  isLoading = false,
  onOpenLeg,
  className,
}: ChainGridProps) {
  const totals = useMemo(() => chainTotals(rows), [rows])

  if (!isLoading && rows.length === 0) {
    return (
      <EmptyState
        className={className}
        title="No strikes at this instant"
        description={
          'The expiry holds no bars at the timestamp selected. Move the scrubber, or download ' +
          'this expiry at this resolution.'
        }
      />
    )
  }

  return (
    <div className={cn('min-w-0 overflow-auto rounded-lg border', className)}>
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 z-10 bg-background">
          <tr className="border-b">
            <th
              colSpan={3}
              className="px-2 py-1 text-left text-[0.65rem] font-medium tracking-wide text-muted-foreground uppercase"
            >
              Calls
            </th>
            <th className="px-2 py-1 text-center text-[0.65rem] font-medium tracking-wide text-muted-foreground uppercase">
              Strike
            </th>
            <th
              colSpan={3}
              className="px-2 py-1 text-right text-[0.65rem] font-medium tracking-wide text-muted-foreground uppercase"
            >
              Puts
            </th>
          </tr>
          <tr className="border-b">
            <th className="px-2 py-1 text-right text-xs font-medium">Last</th>
            <th className="px-2 py-1 text-right text-xs font-medium">Volume</th>
            <th className="px-2 py-1 text-right text-xs font-medium">OI</th>
            <th className="px-2 py-1 text-center text-xs font-medium">
              {spot === null ? '' : 'spot ' + formatStrike(spot)}
            </th>
            <th className="px-2 py-1 text-right text-xs font-medium">Last</th>
            <th className="px-2 py-1 text-right text-xs font-medium">Volume</th>
            <th className="px-2 py-1 text-right text-xs font-medium">OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.strike}
              data-atm={row.is_atm ? 'true' : undefined}
              className={cn(
                'border-b border-border/50',
                // The ATM row carries a background, a left rule and the word ATM next to its
                // strike, so it survives a greyscale screenshot and a colour blind reader alike.
                row.is_atm && 'border-l-2 border-l-primary bg-accent/60 font-medium',
              )}
            >
              <LegCells leg={row.ce} onOpen={onOpenLeg} />
              <td
                className={cn(
                  'border-x px-2 py-1 text-center text-sm tabular-nums',
                  row.is_atm && 'text-foreground',
                )}
              >
                {formatStrike(row.strike)}
                {row.is_atm ? (
                  <span className="ml-1 text-[0.65rem] text-muted-foreground">ATM</span>
                ) : null}
              </td>
              <LegCells leg={row.pe} onOpen={onOpenLeg} />
            </tr>
          ))}
        </tbody>
        <tfoot className="border-t bg-muted/30">
          <tr>
            <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
              {formatCompact(totals.callVolume)}
            </td>
            <td />
            <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
              {formatCompact(totals.callOi)}
            </td>
            <td className="px-2 py-1 text-center text-xs text-muted-foreground">
              {atmStrike === null ? '' : 'ATM ' + formatStrike(atmStrike)}
            </td>
            <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
              {formatCompact(totals.putVolume)}
            </td>
            <td />
            <td className="px-2 py-1 text-right text-xs tabular-nums text-muted-foreground">
              {formatCompact(totals.putOi)}
            </td>
          </tr>
          <tr>
            <td colSpan={7} className="px-2 py-1 text-xs text-muted-foreground">
              {'Put call ratio by open interest: ' +
                (totals.pcr === null ? 'no call open interest' : totals.pcr.toFixed(2)) +
                '. Call OI ' +
                formatInteger(totals.callOi) +
                ', put OI ' +
                formatInteger(totals.putOi) +
                '.'}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  )
}

export default ChainGrid
