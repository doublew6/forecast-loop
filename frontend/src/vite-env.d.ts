/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BASE_PATH?: string
  readonly VITE_ROUTER_MODE?: 'browser' | 'hash'
  readonly VITE_STATIC_DEMO?: 'true' | 'false'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
