import { describe, expect, it, vi } from 'vitest'

import { createApiProxy, normalizeOperatorToken } from './operator-proxy'

describe('operator proxy authentication', () => {
  it('removes client authorization and injects the server-side operator token', () => {
    const token = 'a'.repeat(32)
    const proxy = createApiProxy('http://127.0.0.1:8000', token)
    const on = vi.fn()

    proxy.configure?.({ on } as never, {} as never)
    const handler = on.mock.calls[0]?.[1]
    const proxyRequest = {
      removeHeader: vi.fn(),
      setHeader: vi.fn(),
    }
    handler(proxyRequest)

    expect(proxyRequest.removeHeader).toHaveBeenCalledWith('authorization')
    expect(proxyRequest.setHeader).toHaveBeenCalledWith(
      'Authorization',
      `Bearer ${token}`,
    )
  })

  it('strips client authorization without inventing credentials when unconfigured', () => {
    const proxy = createApiProxy('http://127.0.0.1:8000', undefined)
    const on = vi.fn()

    proxy.configure?.({ on } as never, {} as never)
    const handler = on.mock.calls[0]?.[1]
    const proxyRequest = {
      removeHeader: vi.fn(),
      setHeader: vi.fn(),
    }
    handler(proxyRequest)

    expect(proxyRequest.removeHeader).toHaveBeenCalledWith('authorization')
    expect(proxyRequest.setHeader).not.toHaveBeenCalled()
  })

  it('rejects weak or whitespace-containing tokens without echoing them', () => {
    expect(() => normalizeOperatorToken('short')).toThrow(
      'FORECAST_LOOP_OPERATOR_TOKEN must contain at least 32 non-whitespace characters',
    )
    expect(() => normalizeOperatorToken(`${'a'.repeat(31)} `)).toThrow(
      'FORECAST_LOOP_OPERATOR_TOKEN must contain at least 32 non-whitespace characters',
    )
  })
})
