import type { ClientRequest } from 'node:http'
import type { Plugin, ProxyOptions } from 'vite'

import {
  assertLoopbackServerHost, validateLoopbackProxyTarget,
} from './server-config'

export {
  assertLoopbackServerHost, validateLoopbackProxyTarget, validateOperatorToken,
} from './server-config'

export function applyOperatorAuthorization(
  request: Pick<ClientRequest, 'removeHeader' | 'setHeader'>,
  token: string | undefined,
): void {
  request.removeHeader('authorization')
  if (token) request.setHeader('Authorization', `Bearer ${token}`)
}

export function createLocalApiProxy(target: string, token: string | undefined): ProxyOptions {
  return {
    target: validateLoopbackProxyTarget(target),
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (request) => applyOperatorAuthorization(request, token))
    },
  }
}

export function localOperatorProxyGuard(token: string | undefined, isPreview: boolean): Plugin {
  return {
    name: 'forecast-loop-wiki-local-operator-proxy-guard',
    configResolved(config) {
      if (!token) return
      assertLoopbackServerHost(isPreview ? config.preview.host : config.server.host)
    },
  }
}
