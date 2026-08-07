import { describe, expect, it, vi } from 'vitest'

import {
  applyOperatorAuthorization, assertLoopbackServerHost,
  validateLoopbackProxyTarget, validateOperatorToken,
} from './operator-proxy'

describe('Wiki operator proxy guard', () => {
  it('accepts only credential-free loopback origins', () => {
    const credentialTarget = new URL('http://localhost:8000')
    credentialTarget.username = 'user'
    credentialTarget.password = 'pass'
    expect(validateLoopbackProxyTarget('http://127.0.0.1:8000')).toBe('http://127.0.0.1:8000')
    expect(() => validateLoopbackProxyTarget('https://example.com')).toThrow(/loopback/)
    expect(() => validateLoopbackProxyTarget(credentialTarget.toString())).toThrow(/credential-free/)
    expect(() => validateLoopbackProxyTarget('http://localhost:8000/api')).toThrow(/credential-free/)
  })

  it('rejects short tokens and non-loopback hosts', () => {
    expect(() => validateOperatorToken('short')).toThrow(/32/)
    expect(validateOperatorToken('a'.repeat(32))).toHaveLength(32)
    expect(() => assertLoopbackServerHost('0.0.0.0')).toThrow(/loopback/)
  })

  it('strips browser authorization before injecting the operator token', () => {
    const request = { removeHeader: vi.fn(), setHeader: vi.fn() }
    applyOperatorAuthorization(request, 'x'.repeat(32))
    expect(request.removeHeader).toHaveBeenCalledWith('authorization')
    expect(request.setHeader).toHaveBeenCalledWith('Authorization', `Bearer ${'x'.repeat(32)}`)
  })
})
