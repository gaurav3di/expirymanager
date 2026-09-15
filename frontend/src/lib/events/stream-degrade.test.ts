// @vitest-environment jsdom

// What the event stream does when it drops, and what it must never do to the cache.
//
// The patching functions are covered next door in useEventStream.test.ts. This file mounts the
// hook itself, because the part that can fail quietly is the lifecycle around those functions:
// a stream that dies while the indicator still says Live, a reconnect that resumes from nothing
// and silently skips every frame emitted during the gap, a cache wiped by a transport failure,
// or a closed stream that keeps writing into a cache after the component has gone.
//
// REST is authoritative in this application and the stream is only a refresh accelerator, so the
// standard here is: a dropped stream loses speed and nothing else. Every assertion below is on a
// real result, the status the hook returned or the value sitting in the query cache.

import { createElement, useEffect } from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '@/lib/api/keys'
import type { Budget, Job } from '@/lib/api/types'
import { useEventStream } from '@/lib/events/useEventStream'
import type { EventStreamState } from '@/lib/events/useEventStream'

// React needs to be told it is inside act() before anything renders.
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/** Frames are applied in batches on this cadence, so a test has to pass it to see a patch. */
const FLUSH_INTERVAL_MS = 250

/** The first backoff step the hook takes once the browser has stopped retrying by itself. */
const FIRST_BACKOFF_MS = 1_000

type Listener = (event: MessageEvent<string>) => void

/**
 * A stand in for the browser's EventSource.
 *
 * It is driven by hand rather than by a server, which is what lets a test say "the browser gave
 * up here" and then assert what the application did about it.
 */
class FakeEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static instances: FakeEventSource[] = []

  readonly url: string
  readonly withCredentials: boolean
  readyState = FakeEventSource.CONNECTING
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private readonly listeners = new Map<string, Listener[]>()

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url
    this.withCredentials = init?.withCredentials ?? false
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: Listener): void {
    const bag = this.listeners.get(name) ?? []
    bag.push(listener)
    this.listeners.set(name, bag)
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED
    this.closed = true
  }

  /** The server accepted the connection. */
  connected(): void {
    this.readyState = FakeEventSource.OPEN
    this.onopen?.()
  }

  /** A frame arrived. `raw` is sent verbatim, so a malformed body can be delivered too. */
  deliver(name: string, raw: string, id?: string): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data: raw, lastEventId: id ?? '' } as MessageEvent<string>)
    }
  }

  /** The transport failed. `fatal` is the browser giving up for good rather than retrying. */
  dropped(fatal: boolean): void {
    this.readyState = fatal ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING
    this.onerror?.()
  }
}

function progressJson(done: number): string {
  return JSON.stringify({
    job_id: 'job-1',
    status: 'running',
    total: 100,
    done,
    empty: 0,
    failed: 0,
    skipped: 0,
    requests_used: done,
    rows_written: done * 1000,
    eta_seconds: 60,
  })
}

/** Only the fields these tests read. The full row is built by the API and asserted elsewhere. */
function cachedJob(): Job {
  return {
    job_id: 'job-1',
    kind: 'candle_download',
    status: 'running',
    total_tasks: 100,
    done_tasks: 1,
    empty_tasks: 0,
    failed_tasks: 0,
    skipped_tasks: 0,
    cancelled_tasks: 0,
    pending_tasks: 99,
    leased_tasks: 0,
    requests_used: 1,
    rows_written: 1000,
    eta_seconds: null,
  } as unknown as Job
}

interface Mounted {
  status: () => EventStreamState['status']
  lastEventId: () => string | null
  unmount: () => void
}

let client: QueryClient
const mounted: Array<() => void> = []

function mountStream(options: { enabled?: boolean } = {}): Mounted {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  let latest: EventStreamState = { status: 'idle', lastEventId: null }

  function Probe() {
    const state = useEventStream(options)
    // Recorded from an effect rather than during render: act() flushes effects, so this holds
    // the last committed state by the time a test reads it, and it is not a render side effect.
    useEffect(() => {
      latest = state
    })
    return null
  }

  act(() => {
    root.render(
      createElement(QueryClientProvider, { client }, createElement(Probe, null)),
    )
  })

  const unmount = () => {
    act(() => {
      root.unmount()
    })
  }
  mounted.push(unmount)
  return { status: () => latest.status, lastEventId: () => latest.lastEventId, unmount }
}

function newest(): FakeEventSource {
  const source = FakeEventSource.instances.at(-1)
  if (source === undefined) {
    throw new Error('no stream was opened')
  }
  return source
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.instances = []
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  for (const unmount of mounted.splice(0)) {
    unmount()
  }
  vi.unstubAllGlobals()
  vi.useRealTimers()
  client.clear()
})

describe('while the stream is up', () => {
  it('opens exactly one stream, with the session cookie, and says it is live', () => {
    const stream = mountStream()

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(newest().url).toBe('/api/v1/events/stream')
    // Without this the cookie is not sent and the stream is a 401 the browser cannot retry.
    expect(newest().withCredentials).toBe(true)
    expect(stream.status()).toBe('connecting')

    act(() => {
      newest().connected()
    })
    expect(stream.status()).toBe('open')
  })

  it('patches the query cache with what actually arrived', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), cachedJob())
    mountStream()

    act(() => {
      newest().connected()
      newest().deliver('job_progress', progressJson(42), 'frame-7')
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS)
    })

    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.done_tasks).toBe(42)
  })

  it('drops a frame it cannot read without losing the frame after it', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), cachedJob())
    mountStream()

    act(() => {
      newest().connected()
      newest().deliver('job_progress', 'not json at all', 'frame-8')
      newest().deliver('job_progress', progressJson(51), 'frame-9')
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS)
    })

    // Unreadable is not the same as fatal. The REST equivalent still holds the truth, so the
    // stream carries on rather than tearing itself down.
    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.done_tasks).toBe(51)
  })
})

describe('when the stream drops', () => {
  it('stops claiming to be live while the browser retries on its own', () => {
    const stream = mountStream()
    act(() => {
      newest().connected()
    })

    act(() => {
      newest().dropped(false)
    })

    // The shell shows Polling for anything that is not open, and polls the budget on a timer
    // while it is not open. Reporting open here would leave a stale number on screen with a
    // green dot next to it.
    expect(stream.status()).toBe('reconnecting')
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('reopens after a backoff once the browser has given up, resuming from the last frame', () => {
    const stream = mountStream()
    act(() => {
      newest().connected()
      newest().deliver('job_progress', progressJson(12), 'frame-77')
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS)
    })
    expect(stream.lastEventId()).toBe('frame-77')

    act(() => {
      newest().dropped(true)
    })
    expect(stream.status()).toBe('reconnecting')
    expect(FakeEventSource.instances).toHaveLength(1)

    act(() => {
      vi.advanceTimersByTime(FIRST_BACKOFF_MS)
    })

    expect(FakeEventSource.instances).toHaveLength(2)
    // A reconnect that forgot the id would silently skip everything emitted during the gap.
    expect(newest().url).toBe('/api/v1/events/stream?last_event_id=frame-77')
    expect(FakeEventSource.instances[0].closed).toBe(true)
  })

  it('does not throw away the cache a screen is already showing', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), cachedJob())
    mountStream()
    act(() => {
      newest().connected()
      newest().deliver('job_progress', progressJson(30), 'frame-30')
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS)
    })

    act(() => {
      newest().dropped(true)
      vi.advanceTimersByTime(FIRST_BACKOFF_MS)
    })

    // A transport failure is not news about the job. The numbers stay where they were and the
    // screen keeps rendering them until REST says otherwise.
    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.done_tasks).toBe(30)
  })

  it('resyncs the two live facts on the way back rather than trusting a number from before the gap', () => {
    client.setQueryData(queryKeys.system.budget(), { day_used: 1 } as unknown as Budget)
    mountStream()
    act(() => {
      newest().connected()
    })
    expect(client.getQueryState(queryKeys.system.budget())?.isInvalidated ?? false).toBe(false)

    act(() => {
      newest().dropped(true)
      vi.advanceTimersByTime(FIRST_BACKOFF_MS)
    })
    act(() => {
      newest().connected()
    })

    // Frames emitted during the gap may be past the replay buffer, so the entries that show a
    // live number are marked stale and refetched. Marked, not cleared: the old value keeps
    // rendering until the new one lands.
    expect(client.getQueryState(queryKeys.system.budget())?.isInvalidated).toBe(true)
    expect(client.getQueryData<Budget>(queryKeys.system.budget())?.day_used).toBe(1)
  })
})

describe('when there is no stream to open', () => {
  it('reports unavailable where the browser has no EventSource, and opens nothing', () => {
    vi.stubGlobal('EventSource', undefined)

    const stream = mountStream()

    expect(stream.status()).toBe('unavailable')
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('stays idle while the app is not authenticated yet', () => {
    const stream = mountStream({ enabled: false })

    // Opening the stream logged out earns a 401 the browser cannot retry, and the backoff loop
    // would then hammer it.
    expect(stream.status()).toBe('idle')
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('closes the stream on unmount and drops the frames it had buffered', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), cachedJob())
    const stream = mountStream()
    const source = newest()
    act(() => {
      source.connected()
      // Arrived inside the flush window, so it is still sitting in the buffer at unmount.
      source.deliver('job_progress', progressJson(99), 'frame-99')
    })

    stream.unmount()
    act(() => {
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS * 4)
    })

    expect(source.closed).toBe(true)
    // A buffered frame applied after unmount writes into a cache no screen is reading, and the
    // query client outlives the shell, so it would still be there at the next login.
    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.done_tasks).toBe(1)
  })
})
