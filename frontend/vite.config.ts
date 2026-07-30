import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

import { createApiProxy } from './operator-proxy'

const repositoryRoot = fileURLToPath(new URL('../', import.meta.url))

export default defineConfig(({ mode }) => {
  const fileEnvironment = loadEnv(mode, repositoryRoot, '')
  const environment = {
    ...fileEnvironment,
    ...process.env,
  }
  const apiProxy = createApiProxy(
    environment.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
    environment.FORECAST_LOOP_OPERATOR_TOKEN,
  )

  return {
    base: environment.VITE_BASE_PATH || '/',
    envDir: repositoryRoot,
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        '/api': apiProxy,
      },
    },
    preview: {
      host: '127.0.0.1',
      port: 4173,
      proxy: {
        '/api': apiProxy,
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.ts',
      css: true,
    },
  }
})
