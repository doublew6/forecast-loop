import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  envDir: false,
  publicDir: false,
  plugins: [react()],
  server: { host: '127.0.0.1', port: 4178, strictPort: true },
  preview: { host: '127.0.0.1', port: 4178, strictPort: true },
  build: {
    outDir: 'dist-showcase',
    sourcemap: false,
    rollupOptions: { input: fileURLToPath(new URL('showcase.html', import.meta.url)) },
  },
})
