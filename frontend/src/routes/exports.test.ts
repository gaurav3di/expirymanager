// @vitest-environment jsdom

// The export screen makes two claims that have to be true.
//
//  1. The resolution pills are what has actually been downloaded. They are derived from the
//     coverage ledger for the chosen underlying, not from the fourteen resolutions the broker
//     serves and not from what the underlying is configured to fetch. A resolution that was
//     fetched and came back empty is present and refused, with the reason.
//  2. A finished export is findable. A single file offers a download; a partitioned export says
//     why it cannot and where it is instead; a failed export carries the backend's own sentence.

import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { deliveryFor, describeExportScope } from '@/routes/exports'
import type { ExportRow } from '@/routes/exports'
import {
  buildExportBody,
  estimateScope,
  resolutionOptionsFrom,
} from '@/components/exports/ExportDialog'
import type { CoverageGrid, ExportForm } from '@/components/exports/ExportDialog'

/** res_id 1 is the five second series and res_id 2 is the one minute series, which is exactly
 *  why a Fyers code and a res_id must never be confused on the wire. */
const grid: CoverageGrid = {
  underlying_id: 1,
  resolutions: [
    { res_id: 1, fyers_code: '5S', label: '5 seconds', seconds: 5 },
    { res_id: 2, fyers_code: '1', label: '1 minute', seconds: 60 },
    { res_id: 6, fyers_code: 'D', label: '1 day', seconds: 86400 },
  ],
  cells: [
    {
      expiry_date: '2025-03-27',
      res_id: 2,
      contracts_total: 40,
      contracts_with_data: 36,
      contracts_missing: 4,
      chunks_ok: 100,
      chunks_empty: 4,
      chunks_error: 0,
      rows: 52_873,
    },
    {
      expiry_date: '2025-04-24',
      res_id: 2,
      contracts_total: 40,
      contracts_with_data: 10,
      contracts_missing: 30,
      chunks_ok: 20,
      chunks_empty: 0,
      chunks_error: 1,
      rows: 8_000,
    },
    {
      expiry_date: '2025-03-27',
      res_id: 6,
      contracts_total: 40,
      contracts_with_data: 40,
      contracts_missing: 0,
      chunks_ok: 40,
      chunks_empty: 0,
      chunks_error: 0,
      rows: 400,
    },
    {
      // Fetched, and Fyers had nothing. That is a final answer for an expired contract, so this
      // resolution is downloaded and still holds nothing to export.
      expiry_date: '2025-03-27',
      res_id: 1,
      contracts_total: 40,
      contracts_with_data: 0,
      contracts_missing: 40,
      chunks_ok: 0,
      chunks_empty: 12,
      chunks_error: 0,
      rows: 0,
    },
  ],
}

describe('resolutionOptionsFrom', () => {
  it('offers only what the ledger says was fetched', () => {
    const options = resolutionOptionsFrom(grid)
    expect(options.map((option) => option.fyers_code)).toEqual(['5S', '1', 'D'])
    // Nothing invented: a resolution the grid does not name is simply not there.
    expect(options.some((option) => option.fyers_code === '15')).toBe(false)
  })

  it('sums the rows behind each pill across every expiry', () => {
    const minute = resolutionOptionsFrom(grid).find((option) => option.fyers_code === '1')
    expect(minute?.rows).toBe(60_873)
    expect(minute?.chunks_ok).toBe(120)
    expect(minute?.chunks_error).toBe(1)
    expect(minute?.exportable).toBe(true)
  })

  it('refuses a resolution that was fetched and came back empty, rather than hiding it', () => {
    const seconds = resolutionOptionsFrom(grid).find((option) => option.fyers_code === '5S')
    expect(seconds?.exportable).toBe(false)
    expect(seconds?.rows).toBe(0)
    // The chunk counts survive so the screen can say fetched-and-empty rather than never-fetched.
    expect(seconds?.chunks_empty).toBe(12)
  })

  it('is empty rather than throwing before the grid has loaded', () => {
    expect(resolutionOptionsFrom(undefined)).toEqual([])
  })
})

describe('estimateScope', () => {
  const options = resolutionOptionsFrom(grid)

  it('counts the rows the ledger holds for the chosen resolutions', () => {
    const estimate = estimateScope(grid, options, {
      resolutionCodes: ['1'],
      expiryFrom: '',
      expiryTo: '',
    })
    expect(estimate.rows).toBe(60_873)
    expect(estimate.expiries).toBe(2)
  })

  it('narrows to the expiry window, inclusive at both ends', () => {
    const estimate = estimateScope(grid, options, {
      resolutionCodes: ['1'],
      expiryFrom: '2025-03-27',
      expiryTo: '2025-03-27',
    })
    expect(estimate.rows).toBe(52_873)
    expect(estimate.expiries).toBe(1)
  })

  it('means every resolution held when none is picked, which is what the backend does', () => {
    const estimate = estimateScope(grid, options, {
      resolutionCodes: [],
      expiryFrom: '',
      expiryTo: '',
    })
    expect(estimate.rows).toBe(61_273)
    // The two with rows. The empty one contributes nothing and is not counted as a resolution
    // the export would produce.
    expect(estimate.resolutions).toBe(2)
  })
})

describe('buildExportBody', () => {
  const form: ExportForm = {
    underlyingId: 1,
    resolutionCodes: ['1', 'D'],
    expiryFrom: '2025-03-01',
    expiryTo: '2025-04-30',
    kind: 'OPT',
    optionType: 'CE',
    format: 'parquet',
    layout: 'single',
    compression: 'zstd',
    denormalise: true,
    includeCatalog: false,
  }

  it('sends Fyers codes and never res_ids', () => {
    // res_id 1 is the five second series while the code "1" is one minute. Sending the id as a
    // code would export the wrong series with no error anywhere.
    expect(buildExportBody(form).scope.resolutions).toEqual(['1', 'D'])
  })

  it('sends an empty expiry box as null rather than as an empty string', () => {
    const body = buildExportBody({ ...form, expiryFrom: '', expiryTo: '' })
    expect(body.scope.expiry_from).toBeNull()
    expect(body.scope.expiry_to).toBeNull()
  })

  it('only sends a right when the scope is options', () => {
    expect(buildExportBody(form).scope.option_type).toBe('CE')
    expect(buildExportBody({ ...form, kind: 'FUT' }).scope.option_type).toBeNull()
    expect(buildExportBody({ ...form, optionType: '' }).scope.option_type).toBeNull()
  })

  it('carries the file decisions through unchanged', () => {
    const body = buildExportBody({ ...form, format: 'csv', layout: 'hive', includeCatalog: true })
    expect(body.format).toBe('csv')
    expect(body.layout).toBe('hive')
    expect(body.denormalise).toBe(true)
    expect(body.scope.include_catalog).toBe(true)
  })
})

describe('deliveryFor', () => {
  const base: ExportRow = {
    export_id: 'exp_1',
    job_id: 'job_1',
    status: 'ready',
    format: 'parquet',
    layout: 'single',
    compression: 'zstd',
    row_count: 52_873,
    byte_size: 1_048_576,
    sha256: 'a'.repeat(64),
    created_at: '2025-03-27T10:00:00',
    finished_at: '2025-03-27T10:00:40',
    error_message: null,
    scope: {},
  }

  it('offers a single finished file', () => {
    const delivery = deliveryFor(base)
    expect(delivery.kind).toBe('file')
    expect(delivery.reason).toBeNull()
  })

  it('says why a partitioned export has no download instead of offering one that 409s', () => {
    const delivery = deliveryFor({ ...base, layout: 'hive' })
    expect(delivery.kind).toBe('directory')
    expect(delivery.label).toBe('Ready')
    expect(delivery.reason).toContain('directory of parts')
  })

  it('carries the backend sentence on a failure rather than a generic one', () => {
    const delivery = deliveryFor({
      ...base,
      status: 'failed',
      error_message: 'No rows matched that scope.',
    })
    expect(delivery.kind).toBe('failed')
    expect(delivery.reason).toBe('No rows matched that scope.')
  })

  it('does not offer a file while the export is still being written', () => {
    expect(deliveryFor({ ...base, status: 'running' }).kind).toBe('working')
    expect(deliveryFor({ ...base, status: 'queued' }).kind).toBe('working')
    expect(deliveryFor({ ...base, status: 'deleted' }).kind).toBe('gone')
  })
})

describe('describeExportScope', () => {
  it('reads a narrowed scope back in words', () => {
    expect(
      describeExportScope(
        {
          underlying_id: 1,
          expiry_from: '2025-03-01',
          expiry_to: '2025-04-30',
          resolutions: [2, 6],
          kind: 'OPT',
          option_type: 'CE',
          include_catalog: true,
        },
        'NIFTY 50',
      ),
    ).toBe(
      'NIFTY 50, options, CE only, expiries 2025-03-01 to 2025-04-30, 2 resolutions, with the catalog tables',
    )
  })

  it('says every when a scope narrows nothing', () => {
    expect(describeExportScope({}, null)).toBe(
      'Every underlying, futures and options, every expiry, every resolution held',
    )
  })

  it('counts stored resolutions rather than printing their ids as codes', () => {
    // The scope is stored resolved, as res_ids. Printing 2 where the rest of the app prints "1"
    // for one minute would read as a different resolution entirely.
    const described = describeExportScope({ resolutions: [2] }, 'BANKNIFTY')
    expect(described).toContain('1 resolution')
    expect(described).not.toContain('res_id')
  })
})

describe('the screen uses what these functions produce', () => {
  const routeSource = fs.readFileSync(path.join(import.meta.dirname, 'exports.tsx'), 'utf8')
  const dialogSource = fs.readFileSync(
    path.join(import.meta.dirname, '..', 'components', 'exports', 'ExportDialog.tsx'),
    'utf8',
  )

  it('builds the pills from the coverage grid and not from a list in the file', () => {
    expect(dialogSource).toContain("api.get<CoverageGrid>('/coverage/grid'")
    expect(dialogSource).toContain('resolutionOptionsFrom(grid.data)')
  })

  it('posts the body this test asserts, and nothing assembled a second time', () => {
    expect(dialogSource).toContain('create.mutate(buildExportBody(form))')
  })

  it('renders the delivery decision rather than always drawing a download link', () => {
    expect(routeSource).toContain('const delivery = deliveryFor(row)')
    expect(routeSource).toContain("delivery.kind === 'file'")
  })
})
