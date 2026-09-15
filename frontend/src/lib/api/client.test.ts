// @vitest-environment jsdom

// The client is the only place in the app that talks to the network, so the three things it
// guarantees are worth pinning: the CSRF header on unsafe methods and nowhere else, cookies on
// every request, and a typed error carrying the backend's own code.
//
// Every unsafe request in the application depends on this one header, so the last section of this
// file follows it the whole way: the name and the cookie are read out of the backend's own source
// rather than copied, the rejection body is the one the running backend actually answered with,
// and the end of the chain is the sentence that lands on the screen.
//
// Every token in this file is synthetic. Nothing here is or resembles a real credential.

import fs from 'node:fs'
import path from 'node:path'

import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  api,
  apiRequest,
  apiUrl,
  buildQuery,
  isTerminalApiError,
  onApiEvent,
  readCookie,
  readCsrfToken,
} from '@/lib/api/client'
import { BrokerPanel, apiErrorMessage } from '@/components/settings/BrokerPanel'

const SYNTHETIC_CSRF = 'test-csrf-token-not-a-real-secret'

function setCookie(value: string): void {
  document.cookie = 'em_csrf=' + value + '; path=/'
}

function clearCookies(): void {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim()
    if (name) {
      document.cookie = name + '=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    }
  }
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

function errorResponse(
  status: number,
  code: string,
  extra: { correlation_id?: string; detail?: Record<string, unknown> } = {},
  headers: Record<string, string> = {},
): Response {
  return new Response(
    JSON.stringify({
      error: { code, message: 'safe human text', ...extra },
    }),
    { status, headers: { 'Content-Type': 'application/json', ...headers } },
  )
}

/** A fresh Response per call. A Response body can only be read once, so a mock that resolves
 *  to one shared object fails the second time it is used. */
function mockFetch(factory: () => Response): void {
  vi.mocked(globalThis.fetch).mockImplementation(() => Promise.resolve(factory()))
}

function lastRequest(): { url: string; init: RequestInit } {
  const mock = vi.mocked(globalThis.fetch)
  const call = mock.mock.calls.at(-1)
  if (!call) {
    throw new Error('fetch was not called')
  }
  return { url: String(call[0]), init: (call[1] ?? {}) as RequestInit }
}

function headerOf(name: string): string | undefined {
  const headers = lastRequest().init.headers as Record<string, string> | undefined
  return headers?.[name]
}

beforeEach(() => {
  clearCookies()
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('csrf header', () => {
  it('sends X-CSRF-Token from the em_csrf cookie on every unsafe method', async () => {
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))

    for (const call of [
      () => api.post('/broker/fyers/connect'),
      () => api.patch('/underlyings/1', { body: { is_active: false } }),
      () => api.put('/system/settings', { body: {} }),
      () => api.delete('/exports/abc'),
    ]) {
      await call()
      expect(headerOf('X-CSRF-Token')).toBe(SYNTHETIC_CSRF)
    }
  })

  it('does not send X-CSRF-Token on a safe method', async () => {
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ provisioned: true }))

    await api.get('/bootstrap')

    expect(headerOf('X-CSRF-Token')).toBeUndefined()
  })

  it('reads the cookie fresh on every call, because login rotates it', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    setCookie('first-synthetic-token')
    await api.post('/auth/logout')
    expect(headerOf('X-CSRF-Token')).toBe('first-synthetic-token')

    setCookie('second-synthetic-token')
    await api.post('/auth/logout')
    expect(headerOf('X-CSRF-Token')).toBe('second-synthetic-token')
  })

  it('url decodes the cookie value', async () => {
    setCookie(encodeURIComponent('token with spaces'))
    expect(readCsrfToken()).toBe('token with spaces')
  })

  it('still sends the request when the cookie is absent, and lets the server decide', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')

    // Short circuiting here would report a CSRF failure for what is really an expired session.
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1)
    expect(headerOf('X-CSRF-Token')).toBeUndefined()
  })

  it('sends cookies on every request, including safe ones', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.get('/bootstrap')
    expect(lastRequest().init.credentials).toBe('same-origin')

    await api.post('/auth/logout')
    expect(lastRequest().init.credentials).toBe('same-origin')
  })

  it('sets a json content type only when there is a body', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')
    expect(headerOf('Content-Type')).toBeUndefined()

    await api.post('/auth/login', { body: { username: 'tester' } })
    expect(headerOf('Content-Type')).toBe('application/json')
  })
})

describe('urls', () => {
  it('prefixes every path with the api version', () => {
    expect(apiUrl('/bootstrap')).toBe('/api/v1/bootstrap')
    expect(apiUrl('bootstrap')).toBe('/api/v1/bootstrap')
  })

  it('repeats a key for an array and drops null and undefined', () => {
    expect(buildQuery({ res: ['1', '5'], limit: 100, cursor: null, kind: undefined })).toBe(
      '?res=1&res=5&limit=100',
    )
  })

  it('returns an empty string rather than a bare question mark', () => {
    expect(buildQuery(undefined)).toBe('')
    expect(buildQuery({ cursor: null })).toBe('')
  })
})

describe('errors', () => {
  it('parses the envelope into a typed error carrying the code and correlation id', async () => {
    mockFetch(() =>
      errorResponse(409, 'plan_changed', {
        correlation_id: '8f2c0000',
        detail: { requests_estimated: 4820 },
      })
    )

    const error = await api.post('/downloads').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.code).toBe('plan_changed')
    expect(apiError.status).toBe(409)
    expect(apiError.message).toBe('safe human text')
    expect(apiError.correlationId).toBe('8f2c0000')
    expect(apiError.detail).toEqual({ requests_estimated: 4820 })
  })

  it('reads Retry-After as seconds on a 429', async () => {
    mockFetch(() => errorResponse(429, 'rate_limited', {}, { 'Retry-After': '42' }))

    const error = (await api.get('/bars').catch((thrown: unknown) => thrown)) as ApiError

    expect(error.retryAfterSeconds).toBe(42)
    expect(error.isRateLimited).toBe(true)
  })

  it('publishes a 401 so the shell can route to login', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('unauthenticated', (error) => seen.push(error))
    mockFetch(() => errorResponse(401, 'not_authenticated'))

    await api.get('/auth/me').catch(() => undefined)
    unsubscribe()

    expect(seen).toHaveLength(1)
    expect(seen[0].isUnauthenticated).toBe(true)
  })

  it('publishes a 409 needs_reauth so the banner can be raised', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('needs_reauth', (error) => seen.push(error))
    mockFetch(() => errorResponse(409, 'needs_reauth'))

    await api.post('/downloads/plan').catch(() => undefined)
    unsubscribe()

    expect(seen).toHaveLength(1)
    expect(seen[0].isNeedsReauth).toBe(true)
  })

  it('stops publishing once the listener unsubscribes', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('unauthenticated', (error) => seen.push(error))
    unsubscribe()
    mockFetch(() => errorResponse(401, 'not_authenticated'))

    await api.get('/auth/me').catch(() => undefined)

    expect(seen).toHaveLength(0)
  })

  it('reports an unreachable backend as a network error rather than a server error', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(new TypeError('Failed to fetch'))

    const error = (await api.get('/bootstrap').catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(0)
    expect(error.code).toBe('network_error')
    expect(error.isNetworkError).toBe(true)
  })

  it('rethrows an abort unchanged, because a cancelled query is not a failure', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(
      new DOMException('The operation was aborted', 'AbortError'),
    )

    const error = await api.get('/bars').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(DOMException)
    expect(error).not.toBeInstanceOf(ApiError)
  })

  it('falls back to a status code when the body is not the documented envelope', async () => {
    mockFetch(() => new Response('gateway exploded', { status: 502 }))

    const error = (await api.get('/bootstrap').catch((thrown: unknown) => thrown)) as ApiError

    expect(error.code).toBe('http_502')
    expect(error.correlationId).toBeNull()
  })

  it('treats a client error as terminal and a rate limit or server error as retryable', () => {
    expect(isTerminalApiError(new ApiError({ status: 401, code: 'x', message: 'm' }))).toBe(true)
    expect(isTerminalApiError(new ApiError({ status: 422, code: 'x', message: 'm' }))).toBe(true)
    expect(isTerminalApiError(new ApiError({ status: 429, code: 'x', message: 'm' }))).toBe(false)
    expect(isTerminalApiError(new ApiError({ status: 503, code: 'x', message: 'm' }))).toBe(false)
    expect(isTerminalApiError(new Error('not an api error'))).toBe(false)
  })
})

describe('responses', () => {
  it('returns undefined for a 204 rather than failing to parse an empty body', async () => {
    mockFetch(() => new Response(null, { status: 204 }))

    await expect(apiRequest<void>('POST', '/auth/logout')).resolves.toBeUndefined()
  })

  it('parses a json body', async () => {
    mockFetch(() => jsonResponse({ provisioned: true }))

    await expect(api.get<{ provisioned: boolean }>('/bootstrap')).resolves.toEqual({
      provisioned: true,
    })
  })
})

// ---------------------------------------------------------------------------
// The CSRF chain, from the backend's constants to the sentence on the screen
// ---------------------------------------------------------------------------

/** The repository root, so the backend's own source can be read rather than paraphrased. */
const backendDir = path.resolve(import.meta.dirname, '..', '..', '..', '..', 'backend')

function backendSource(...parts: string[]): string {
  return fs.readFileSync(path.join(backendDir, 'expirymanager', ...parts), 'utf8')
}

function setNamedCookie(name: string, value: string): void {
  document.cookie = name + '=' + value + '; path=/'
}

/** Every header on the last request, keyed lower case, which is how they travel on the wire. */
function lowerCaseHeaders(): Record<string, string> {
  const headers = (lastRequest().init.headers ?? {}) as Record<string, string>
  return Object.fromEntries(Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value]))
}

describe('the csrf contract with the backend', () => {
  it('sends the header name the backend reads, under the cookie name the backend sets', async () => {
    // Both ends of a synchronizer token have to agree on two strings. Reading them out of the
    // backend rather than restating them here means a rename there fails this test instead of
    // turning every unsafe request in the app into a 403 at runtime.
    const headerName = /CSRF_HEADER_NAME = "([^"]+)"/.exec(backendSource('security', 'csrf.py'))?.[1]
    const cookieName = /CSRF_COOKIE_NAME = "([^"]+)"/.exec(
      backendSource('security', 'sessions.py'),
    )?.[1]
    expect(headerName).toBeTruthy()
    expect(cookieName).toBeTruthy()

    setNamedCookie(cookieName as string, SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))
    await api.post('/broker/fyers/connect')

    expect(readCsrfToken()).toBe(SYNTHETIC_CSRF)
    expect(lowerCaseHeaders()[headerName as string]).toBe(SYNTHETIC_CSRF)
  })

  it('does not mistake another cookie whose name merely ends in the same text', async () => {
    setNamedCookie('other_em_csrf', 'a-different-synthetic-value')
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')

    expect(headerOf('X-CSRF-Token')).toBe(SYNTHETIC_CSRF)
  })

  it('reads nothing at all when only the look alike cookie is present', async () => {
    setNamedCookie('other_em_csrf', 'a-different-synthetic-value')
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')

    expect(readCsrfToken()).toBeNull()
    // Absent, not an empty string. An empty header is a value the backend would compare and
    // reject with a different reason than the one that is true.
    expect('X-CSRF-Token' in ((lastRequest().init.headers ?? {}) as Record<string, string>)).toBe(
      false,
    )
  })

  it('keeps the token out of the url, where it would land in logs and history', async () => {
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/exports', { body: { kind: 'csv' }, query: { dry_run: true } })

    expect(lastRequest().url).toBe('/api/v1/exports?dry_run=true')
    expect(lastRequest().url).not.toContain(SYNTHETIC_CSRF)
  })

  it('never echoes the session cookie anywhere, only the token cookie', async () => {
    // em_session is HttpOnly in the browser, so script cannot read it at all. This pins the rule
    // for the one environment where it can: nothing but em_csrf is ever put into a header.
    setNamedCookie('em_session', 'synthetic-session-value')
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')

    expect(readCookie('em_session')).toBe('synthetic-session-value')
    const sent = Object.values(lastRequest().init.headers as Record<string, string>)
    expect(sent).not.toContain('synthetic-session-value')
    expect(sent).toContain(SYNTHETIC_CSRF)
  })
})

describe('a rejected unsafe request', () => {
  /** The body the running backend actually answered a POST with no token with, recorded from
   *  127.0.0.1:8000. Only the correlation id is replaced, with a synthetic one. */
  const MEASURED_CSRF_REJECTION = {
    error: {
      code: 'csrf_invalid',
      message: 'The request could not be verified. Reload the page and try again.',
      correlation_id: 'c0ffee1234567890',
    },
  }

  function rejectWithMeasuredBody(): void {
    mockFetch(
      () =>
        new Response(JSON.stringify(MEASURED_CSRF_REJECTION), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        }),
    )
  }

  it('carries the backend reason code rather than a generic failure', async () => {
    rejectWithMeasuredBody()

    const error = (await api.post('/downloads').catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(403)
    expect(error.code).toBe('csrf_invalid')
    expect(error.correlationId).toBe('c0ffee1234567890')
  })

  it('is not reported as an expired session, which would throw the user out of the page', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('unauthenticated', (error) => seen.push(error))
    rejectWithMeasuredBody()

    await api.post('/downloads').catch(() => undefined)
    unsubscribe()

    expect(seen).toHaveLength(0)
  })

  it('is not retried, because a request the backend refused to verify cannot succeed by repeating', async () => {
    const error = new ApiError({ status: 403, code: 'csrf_invalid', message: 'x' })
    expect(isTerminalApiError(error)).toBe(true)
  })

  it('becomes a sentence with a next step in it, and a reference to quote', async () => {
    rejectWithMeasuredBody()

    const error = await api.post('/broker/fyers/credentials', { body: {} }).catch((e: unknown) => e)
    const sentence = apiErrorMessage(error)

    expect(sentence).toBe(
      'The request could not be verified. Reload the page and try again. Reference c0ffee1234567890.',
    )
  })

  it('renders that sentence on the screen instead of failing quietly', async () => {
    rejectWithMeasuredBody()
    const error = await api.post('/broker/fyers/credentials', { body: {} }).catch((e: unknown) => e)

    const markup = renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        createElement(BrokerPanel, { status: undefined, error }),
      ),
    )

    expect(markup).toContain('The request could not be verified. Reload the page and try again.')
    expect(markup).toContain('Reference c0ffee1234567890.')
  })

  it('repeats every rejection reason the backend has, word for word', () => {
    // The middleware answers with one of a small set of safe sentences. Whichever one it chooses,
    // the user sees that sentence: the client does not translate it, and does not replace it with
    // a code.
    const source = backendSource('security', 'csrf.py')
    const messages = [...source.matchAll(/REASON_[A-Z_]+: "([^"]+)"/g)].map((match) => match[1])
    expect(messages.length).toBeGreaterThanOrEqual(3)

    for (const message of messages) {
      const error = new ApiError({ status: 403, code: 'cross_origin_rejected', message })
      expect(apiErrorMessage(error)).toBe(message)
    }
  })
})
