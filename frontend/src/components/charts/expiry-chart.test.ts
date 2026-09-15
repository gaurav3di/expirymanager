// The React rules for mounting this widget are not style preferences. Each one has a failure:
// a rebuilt terminal on every parent render, a chart holding a running animation frame loop in
// React state, a leaked canvas on unmount, paging that dies for the rest of the session.
//
// None of that is observable from a rendered tree without a canvas, so this file reads the source
// the way the connect tab test does and pins the shape.

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { oldestBarTime } from '@/components/charts/ExpiryChart'

const source = fs.readFileSync(path.join(import.meta.dirname, 'ExpiryChart.tsx'), 'utf8')
const code = source.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')

describe('the mount effect', () => {
  it('creates the widget once, with an empty dependency array', () => {
    const start = code.indexOf('createWidget(')
    expect(start).toBeGreaterThan(-1)
    // The effect that contains createWidget must close with `}, [])`. Symbol, interval and theme
    // in that array would rebuild the terminal and throw away the user's indicators and drawings.
    const after = code.slice(start)
    const close = after.indexOf('}, [')
    expect(close).toBeGreaterThan(-1)
    expect(after.slice(close, close + 6)).toBe('}, [])')
  })

  it('destroys the widget in the cleanup', () => {
    expect(code).toContain('widget.destroy()')
  })

  it('holds the widget in a ref and never in state', () => {
    expect(code).toContain('useRef<Widget | null>(null)')
    expect(code).not.toContain('useState<Widget')
  })

  it('drives symbol, interval and theme through the imperative setters', () => {
    expect(code).toContain('setSymbol(symbol, exchange)')
    expect(code).toContain('setInterval(interval)')
    expect(code).toContain('setTheme(theme)')
  })

  it('imports the indicators tier bare, and never deep imports into dist', () => {
    // A deep import creates a second registry instance, and addIndicator then throws on a page
    // that plainly imported the tier.
    expect(source).toContain("import 'openalgo-charts/indicators'")
    expect(source).not.toContain('openalgo-charts/dist')
    expect(source).not.toContain('node_modules')
  })

  it('gives the container a resolved height', () => {
    // height 100% inside an auto height parent is zero pixels and nothing paints at all.
    expect(code).toContain('min-h-[480px]')
  })

  it('omits width and height props and adds no resize listener of its own', () => {
    // The chart installs its own ResizeObserver; a second one fights it.
    expect(code).not.toContain('addEventListener(\'resize\'')
    expect(code).not.toContain('window.addEventListener')
  })
})

describe('the history loader', () => {
  it('calls historyLoadComplete from a finally, so no exit path can skip it', () => {
    // A latch suppresses re-entry until it is called. Missing it once on the empty path or the
    // error path kills paging for the rest of the session.
    const start = code.indexOf('setHistoryLoader(')
    expect(start).toBeGreaterThan(-1)
    const body = code.slice(start, code.indexOf('return () => {', start))
    expect(body).toContain('finally {')
    const finallyBlock = body.slice(body.indexOf('finally {'))
    expect(finallyBlock).toContain('historyLoadComplete()')
  })

  it('restores the viewport after prepending, because prependData shifts every index', () => {
    expect(code).toContain('getVisibleLogicalRange()')
    expect(code).toContain('setVisibleLogicalRange({')
    expect(code).toContain('before.from + older.length')
  })
})

describe('oldestBarTime', () => {
  it('names the time paging must continue from', () => {
    expect(oldestBarTime([{ time: 100 }, { time: 200 }])).toBe(100)
  })

  it('has no answer for an empty series, so no request is made for one', () => {
    expect(oldestBarTime([])).toBeNull()
  })
})
