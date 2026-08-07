import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

import { createLocalApiProxy, localOperatorProxyGuard, validateOperatorToken } from './operator-proxy'

const repositoryRoot = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig(({ command, isPreview = false, mode }) => {
  const publicEnvironment = loadEnv(mode, repositoryRoot, 'VITE_')
  const environmentValue = (name: string) => process.env[name] ?? publicEnvironment[name]
  const target = environmentValue('VITE_API_PROXY_TARGET') || 'http://127.0.0.1:8000'
  let token: string | undefined
  if (command === 'serve') {
    const privateEnvironment = loadEnv(mode, repositoryRoot, 'FORECAST_LOOP_OPERATOR_TOKEN')
    token = validateOperatorToken(process.env.FORECAST_LOOP_OPERATOR_TOKEN
      ?? privateEnvironment.FORECAST_LOOP_OPERATOR_TOKEN)
  }
  const proxy = createLocalApiProxy(target, token)
  return {
    base: environmentValue('VITE_WIKI_BASE_PATH') || '/',
    plugins: [react(), localOperatorProxyGuard(token, isPreview)],
    server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy: { '/api': proxy } },
    preview: { host: '127.0.0.1', port: 4174, strictPort: true, proxy: { '/api': proxy } },
    test: { environment: 'jsdom', globals: true, setupFiles: './src/test/setup.ts', css: true },
  }
})
