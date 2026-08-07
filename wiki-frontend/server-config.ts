const LOOPBACK_HOSTS = new Set(['127.0.0.1', '[::1]', '::1', 'localhost'])

export function validateLoopbackProxyTarget(value: string): string {
  let target: URL
  try {
    target = new URL(value)
  } catch {
    throw new Error('VITE_API_PROXY_TARGET must be a valid loopback URL')
  }
  if (
    !['http:', 'https:'].includes(target.protocol)
    || !LOOPBACK_HOSTS.has(target.hostname.toLowerCase())
    || target.username || target.password || target.search || target.hash
    || target.pathname !== '/'
  ) {
    throw new Error('VITE_API_PROXY_TARGET must be a credential-free HTTP(S) loopback origin')
  }
  return value
}

export function validateOperatorToken(value: string | undefined): string | undefined {
  if (!value) return undefined
  if (value.length < 32 || /\s/.test(value)) {
    throw new Error('FORECAST_LOOP_OPERATOR_TOKEN must be at least 32 non-whitespace characters')
  }
  return value
}

export function validatePublicOrigin(value: string): string {
  let origin: URL
  try {
    origin = new URL(value)
  } catch {
    throw new Error('WIKI_PUBLIC_ORIGIN must be a valid URL')
  }
  if (
    origin.protocol !== 'https:'
    || origin.username || origin.password || origin.search || origin.hash
    || origin.pathname !== '/'
  ) {
    throw new Error('WIKI_PUBLIC_ORIGIN must be a credential-free HTTPS origin')
  }
  return origin.origin
}

export function validatePublicUsername(value: string): string {
  if (!/^[A-Za-z0-9._-]{3,64}$/.test(value)) {
    throw new Error('WIKI_USERNAME must contain 3-64 ASCII letters, digits, dots, underscores, or hyphens')
  }
  return value
}

export function validatePort(value: string | undefined, fallback: number): number {
  const port = value === undefined ? fallback : Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('WIKI_PORT must be an integer from 1 to 65535')
  }
  return port
}

export function assertLoopbackServerHost(host: string | boolean | undefined): void {
  const normalized = typeof host === 'string' ? host.toLowerCase() : host
  if (normalized === undefined || (typeof normalized === 'string' && LOOPBACK_HOSTS.has(normalized))) return
  throw new Error('The authenticated Wiki proxy must bind to loopback')
}
