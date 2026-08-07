import { createHash } from 'node:crypto'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

import { parse as parseBasicAuthorization } from 'basic-auth'
import dotenv from 'dotenv'
import express, { type NextFunction, type Request, type Response } from 'express'
import helmet from 'helmet'
import { createProxyMiddleware, fixRequestBody } from 'http-proxy-middleware'

import { safeStringEqual, validatePasswordHash, verifyPassword } from './auth-crypto.js'
import {
  assertLoopbackServerHost, validateLoopbackProxyTarget, validateOperatorToken,
  validatePort, validatePublicOrigin, validatePublicUsername,
} from './server-config.js'

const AUTH_REALM = 'Forecast Loop Wiki'
const FAILURE_BODY = { detail: '账户或密码不正确' }
const RATE_LIMIT_BODY = { detail: '登录失败次数过多，请稍后再试' }
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const CREDENTIAL_FAILURE_LIMIT = 5
const CREDENTIAL_FAILURE_WINDOW_MS = 30 * 60 * 1000
const IP_FAILURE_LIMIT = 20
const IP_FAILURE_WINDOW_MS = 60 * 60 * 1000

interface FailureBucket {
  count: number
  resetAt: number
}

interface BlockState {
  blocked: boolean
  retryAfterSeconds: number
}

class LoginFailureTracker {
  private readonly byCredential = new Map<string, FailureBucket>()
  private readonly byIp = new Map<string, FailureBucket>()

  private activeBucket(store: Map<string, FailureBucket>, key: string, now: number): FailureBucket | undefined {
    const bucket = store.get(key)
    if (bucket && bucket.resetAt <= now) {
      store.delete(key)
      return undefined
    }
    return bucket
  }

  private increment(
    store: Map<string, FailureBucket>, key: string, windowMs: number, now: number,
  ): { countBefore: number; resetAt: number } {
    const current = this.activeBucket(store, key, now)
    if (current) {
      const countBefore = current.count
      current.count += 1
      return { countBefore, resetAt: current.resetAt }
    }
    const resetAt = now + windowMs
    store.set(key, { count: 1, resetAt })
    return { countBefore: 0, resetAt }
  }

  check(ipKey: string, credentialKey: string, now = Date.now()): BlockState {
    const ip = this.activeBucket(this.byIp, ipKey, now)
    const credential = this.activeBucket(this.byCredential, credentialKey, now)
    const resetAt = Math.max(
      ip && ip.count >= IP_FAILURE_LIMIT ? ip.resetAt : 0,
      credential && credential.count >= CREDENTIAL_FAILURE_LIMIT ? credential.resetAt : 0,
    )
    return {
      blocked: resetAt > 0,
      retryAfterSeconds: resetAt > 0 ? Math.max(1, Math.ceil((resetAt - now) / 1000)) : 0,
    }
  }

  record(ipKey: string, credentialKey: string, now = Date.now()): BlockState {
    const ip = this.increment(this.byIp, ipKey, IP_FAILURE_WINDOW_MS, now)
    const credential = this.increment(
      this.byCredential, credentialKey, CREDENTIAL_FAILURE_WINDOW_MS, now,
    )
    const resetAt = Math.max(
      ip.countBefore >= IP_FAILURE_LIMIT ? ip.resetAt : 0,
      credential.countBefore >= CREDENTIAL_FAILURE_LIMIT ? credential.resetAt : 0,
    )
    return {
      blocked: resetAt > 0,
      retryAfterSeconds: resetAt > 0 ? Math.max(1, Math.ceil((resetAt - now) / 1000)) : 0,
    }
  }

  clearCredential(credentialKey: string): void {
    this.byCredential.delete(credentialKey)
  }
}

export interface PublicWikiOptions {
  apiTarget: string
  distDir: string
  operatorToken: string
  passwordHash: string
  port: number
  publicOrigin: string
  username: string
}

function credentialFrom(request: Request): { name: string; pass: string } | undefined {
  try {
    const authorization = request.get('authorization')
    return authorization ? parseBasicAuthorization(authorization) : undefined
  } catch {
    return undefined
  }
}

function clientKey(request: Request): string {
  return createHash('sha256')
    .update(request.ip ?? request.socket.remoteAddress ?? 'unknown')
    .digest('base64url')
}

function loginKey(ipKey: string, name: string): string {
  const nameHash = createHash('sha256').update(name).digest('base64url')
  return `${ipKey}:${nameHash}`
}

function sendAuthChallenge(response: Response): void {
  response.setHeader('WWW-Authenticate', `Basic realm="${AUTH_REALM}", charset="UTF-8"`)
  response.status(401).json(FAILURE_BODY)
}

function sendRateLimit(response: Response, retryAfterSeconds: number): void {
  response.setHeader('Retry-After', String(retryAfterSeconds))
  response.status(429).json(RATE_LIMIT_BODY)
}

function normalizedHost(request: Request): string {
  return (request.get('host') ?? '').trim().toLowerCase()
}

function allowedHosts(publicOrigin: string, port: number): Set<string> {
  const publicHost = new URL(publicOrigin).host.toLowerCase()
  return new Set([
    publicHost,
    `127.0.0.1:${port}`,
    `localhost:${port}`,
    `[::1]:${port}`,
  ])
}

function allowedOrigins(publicOrigin: string, port: number): Set<string> {
  return new Set([
    publicOrigin,
    `http://127.0.0.1:${port}`,
    `http://localhost:${port}`,
    `http://[::1]:${port}`,
  ])
}

function sameOriginWriteGuard(publicOrigin: string, port: number) {
  const origins = allowedOrigins(publicOrigin, port)
  return (request: Request, response: Response, next: NextFunction): void => {
    if (!UNSAFE_METHODS.has(request.method)) return next()
    if (request.get('x-forecast-request') !== 'same-origin') {
      response.status(403).json({ detail: '缺少同源写入校验' })
      return
    }
    const fetchSite = request.get('sec-fetch-site')
    if (fetchSite && fetchSite !== 'same-origin') {
      response.status(403).json({ detail: '拒绝跨站写入' })
      return
    }
    const origin = request.get('origin')
    if (origin && !origins.has(origin)) {
      response.status(403).json({ detail: '拒绝未知来源写入' })
      return
    }
    next()
  }
}

function sanitizeProxyRequest(
  proxyRequest: import('node:http').ClientRequest,
  request: IncomingMessage,
  operatorToken: string,
): void {
  for (const header of [
    'authorization', 'proxy-authorization', 'cookie', 'forwarded',
    'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto',
  ]) {
    proxyRequest.removeHeader(header)
  }
  proxyRequest.setHeader('Authorization', `Bearer ${operatorToken}`)
  fixRequestBody(proxyRequest, request)
}

export function createPublicWikiApp(options: PublicWikiOptions) {
  const apiTarget = validateLoopbackProxyTarget(options.apiTarget)
  const operatorToken = validateOperatorToken(options.operatorToken)
  if (!operatorToken) throw new Error('FORECAST_LOOP_OPERATOR_TOKEN is required')
  const passwordHash = validatePasswordHash(options.passwordHash)
  const publicOrigin = validatePublicOrigin(options.publicOrigin)
  const username = validatePublicUsername(options.username)
  const hosts = allowedHosts(publicOrigin, options.port)
  const failures = new LoginFailureTracker()
  const app = express()

  app.disable('x-powered-by')
  app.set('trust proxy', 'loopback')
  app.use(helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'"],
        styleSrc: ["'self'"],
        connectSrc: ["'self'"],
        imgSrc: ["'self'", 'data:'],
        baseUri: ["'none'"],
        frameAncestors: ["'none'"],
        formAction: ["'self'"],
        objectSrc: ["'none'"],
      },
    },
    crossOriginEmbedderPolicy: false,
    hsts: false,
    referrerPolicy: { policy: 'no-referrer' },
  }))
  app.use((_request, response, next) => {
    response.setHeader('Cache-Control', 'no-store')
    response.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
    response.setHeader('X-Robots-Tag', 'noindex, nofollow, noarchive')
    next()
  })
  app.use((request, response, next) => {
    if (hosts.has(normalizedHost(request))) return next()
    response.status(421).json({ detail: '未知的访问主机' })
  })
  app.use((request, response, next) => {
    if (!['CONNECT', 'TRACE'].includes(request.method)) return next()
    response.status(405).json({ detail: '不支持的请求方法' })
  })

  app.use(async (request, response, next) => {
    const authorization = request.get('authorization')
    if (!authorization) {
      sendAuthChallenge(response)
      return
    }
    const credential = credentialFrom(request)
    const ipKey = clientKey(request)
    const credentialKey = loginKey(ipKey, credential?.name.toLowerCase() ?? '<malformed>')
    const blocked = failures.check(ipKey, credentialKey)
    if (blocked.blocked) {
      sendRateLimit(response, blocked.retryAfterSeconds)
      return
    }
    if (!credential) {
      const afterFailure = failures.record(ipKey, credentialKey)
      if (afterFailure.blocked) sendRateLimit(response, afterFailure.retryAfterSeconds)
      else sendAuthChallenge(response)
      return
    }
    try {
      const [passwordMatches] = await Promise.all([
        verifyPassword(credential.pass, passwordHash),
      ])
      const usernameMatches = safeStringEqual(credential.name, username)
      if (!passwordMatches || !usernameMatches) {
        const afterFailure = failures.record(ipKey, credentialKey)
        if (afterFailure.blocked) sendRateLimit(response, afterFailure.retryAfterSeconds)
        else sendAuthChallenge(response)
        return
      }
      failures.clearCredential(credentialKey)
      response.locals.authenticated = true
      next()
    } catch {
      response.status(503).json({ detail: '认证服务暂时不可用' })
    }
  })

  app.use(sameOriginWriteGuard(publicOrigin, options.port))
  app.use('/api', express.json({ limit: '28mb', strict: true, type: ['application/json', 'application/*+json'] }))
  app.use(createProxyMiddleware<Request, Response>({
    pathFilter: '/api',
    target: apiTarget,
    changeOrigin: true,
    followRedirects: false,
    proxyTimeout: 30_000,
    timeout: 35_000,
    xfwd: false,
    on: {
      proxyReq(proxyRequest, request) {
        sanitizeProxyRequest(proxyRequest, request, operatorToken)
      },
      proxyRes(proxyResponse) {
        delete proxyResponse.headers['access-control-allow-credentials']
        delete proxyResponse.headers['access-control-allow-origin']
        delete proxyResponse.headers['server']
        delete proxyResponse.headers['set-cookie']
      },
      error(_error, _request, response) {
        const outgoing = response as ServerResponse
        if (!outgoing.headersSent) {
          outgoing.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' })
        }
        outgoing.end(JSON.stringify({ detail: '本地 Wiki API 暂时不可用' }))
      },
    },
  }))

  app.use('/assets', express.static(join(options.distDir, 'assets'), {
    dotfiles: 'deny', immutable: true, index: false, maxAge: '1y', redirect: false,
  }))
  app.use(express.static(options.distDir, {
    dotfiles: 'deny', index: false, maxAge: 0, redirect: false,
  }))
  app.get(/.*/, (_request, response) => {
    response.sendFile(join(options.distDir, 'index.html'))
  })
  app.use((_request, response) => {
    response.status(404).json({ detail: '未找到请求资源' })
  })
  app.use((error: unknown, _request: Request, response: Response, next: NextFunction) => {
    void next
    const status = typeof error === 'object' && error && 'status' in error && Number(error.status) === 413 ? 413 : 400
    response.status(status).json({ detail: status === 413 ? '请求体过大' : '请求格式不正确' })
  })
  return app
}

function loadRuntimeOptions(): PublicWikiOptions {
  const compiledDirectory = dirname(fileURLToPath(import.meta.url))
  const frontendRoot = resolve(compiledDirectory, '..')
  const repositoryRoot = resolve(frontendRoot, '..')
  dotenv.config({ path: join(repositoryRoot, '.env'), quiet: true })
  dotenv.config({ path: join(repositoryRoot, 'data/private/wiki-web.env'), override: true, quiet: true })
  const port = validatePort(process.env.WIKI_PORT, 4174)
  return {
    apiTarget: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
    distDir: join(frontendRoot, 'dist'),
    operatorToken: process.env.FORECAST_LOOP_OPERATOR_TOKEN ?? '',
    passwordHash: process.env.WIKI_PASSWORD_HASH ?? '',
    port,
    publicOrigin: process.env.WIKI_PUBLIC_ORIGIN ?? '',
    username: process.env.WIKI_USERNAME ?? '',
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const options = loadRuntimeOptions()
  const host = '127.0.0.1'
  assertLoopbackServerHost(host)
  const server = createServer({ maxHeaderSize: 16 * 1024 }, createPublicWikiApp(options))
  server.headersTimeout = 15_000
  server.requestTimeout = 40_000
  server.keepAliveTimeout = 5_000
  server.on('clientError', (_error, socket) => {
    if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n')
  })
  server.listen(options.port, host)
}
