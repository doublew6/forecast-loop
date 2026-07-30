import type { ProxyOptions } from 'vite'

const MINIMUM_OPERATOR_TOKEN_LENGTH = 32

export function normalizeOperatorToken(rawToken: string | undefined): string | undefined {
  if (rawToken === undefined || rawToken === '') return undefined
  if (
    rawToken.length < MINIMUM_OPERATOR_TOKEN_LENGTH
    || /\s/.test(rawToken)
  ) {
    throw new Error(
      'FORECAST_LOOP_OPERATOR_TOKEN must contain at least 32 non-whitespace characters',
    )
  }
  return rawToken
}

export function createApiProxy(
  target: string,
  rawOperatorToken: string | undefined,
): ProxyOptions {
  const operatorToken = normalizeOperatorToken(rawOperatorToken)

  return {
    target,
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (proxyRequest) => {
        proxyRequest.removeHeader('authorization')
        if (operatorToken !== undefined) {
          proxyRequest.setHeader('Authorization', `Bearer ${operatorToken}`)
        }
      })
    },
  }
}
