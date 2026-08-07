import { createServer } from 'node:http'
import { type AddressInfo } from 'node:net'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import request from 'supertest'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { createPasswordHash } from './auth-crypto.js'
import { createPublicWikiApp } from './server.js'

const USERNAME = 'wiki-admin'
const PASSWORD = 'correct-horse-battery-staple-2026'
const HOST = '127.0.0.1:4174'

interface ProxyCapture {
  authorization?: string
  body?: string
  cookie?: string
  path?: string
}

describe('public Wiki security gateway', () => {
  let apiTarget = ''
  let application: ReturnType<typeof createPublicWikiApp>
  let capture: ProxyCapture = {}
  let distDirectory = ''
  const backend = createServer((incoming, response) => {
    const chunks: Buffer[] = []
    incoming.on('data', (chunk: Buffer) => chunks.push(chunk))
    incoming.on('end', () => {
      capture = {
        authorization: incoming.headers.authorization,
        body: Buffer.concat(chunks).toString('utf8'),
        cookie: incoming.headers.cookie,
        path: incoming.url,
      }
      response.writeHead(200, { 'Content-Type': 'application/json', 'Set-Cookie': 'backend=secret' })
      response.end(JSON.stringify({ ok: true }))
    })
  })

  beforeAll(async () => {
    await new Promise<void>((resolve) => backend.listen(0, '127.0.0.1', resolve))
    const address = backend.address() as AddressInfo
    apiTarget = `http://127.0.0.1:${address.port}`
    distDirectory = await mkdtemp(join(tmpdir(), 'wiki-gateway-test-'))
    await mkdir(join(distDirectory, 'assets'))
    await writeFile(join(distDirectory, 'index.html'), '<!doctype html><title>Wiki Atlas</title>')
    await writeFile(join(distDirectory, 'assets/app.js'), 'export {}')
    application = createPublicWikiApp({
      apiTarget,
      distDir: distDirectory,
      operatorToken: 'operator-token-that-is-at-least-thirty-two-characters',
      passwordHash: await createPasswordHash(PASSWORD),
      port: 4174,
      publicOrigin: 'https://wiki.example.test',
      username: USERNAME,
    })
  })

  afterAll(async () => {
    await new Promise<void>((resolve, reject) => backend.close((error) => error ? reject(error) : resolve()))
    await rm(distDirectory, { recursive: true, force: true })
  })

  it('challenges unauthenticated requests without weakening browser security headers', async () => {
    const response = await request(application).get('/').set('Host', HOST)
    expect(response.status).toBe(401)
    expect(response.headers['www-authenticate']).toContain('Basic realm="Forecast Loop Wiki"')
    expect(response.headers['content-security-policy']).toContain("default-src 'self'")
    expect(response.headers['strict-transport-security']).toBeUndefined()
    expect(response.headers['x-robots-tag']).toContain('noindex')
  })

  it('serves the Wiki shell after valid authentication', async () => {
    const response = await request(application)
      .get('/')
      .set('Host', HOST)
      .auth(USERNAME, PASSWORD, { type: 'basic' })
    expect(response.status).toBe(200)
    expect(response.text).toContain('Wiki Atlas')
  })

  it('does not count concurrent successful API requests as login failures', async () => {
    const responses = await Promise.all(Array.from({ length: 7 }, () => (
      request(application)
        .get('/api/health')
        .set('Host', HOST)
        .auth(USERNAME, PASSWORD, { type: 'basic' })
    )))
    expect(responses.map((response) => response.status)).toEqual(Array(7).fill(200))
  })

  it('blocks the sixth failed attempt for one username and IP', async () => {
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      const response = await request(application)
        .get('/')
        .set('Host', HOST)
        .auth('wrong-user', 'wrong-password', { type: 'basic' })
      expect(response.status).toBe(401)
    }
    const blocked = await request(application)
      .get('/')
      .set('Host', HOST)
      .auth('wrong-user', 'wrong-password', { type: 'basic' })
    expect(blocked.status).toBe(429)
    expect(blocked.body.detail).toMatch(/次数过多/)
    expect(Number(blocked.headers['retry-after'])).toBeGreaterThan(0)
  })

  it('requires a same-origin marker on writes', async () => {
    const response = await request(application)
      .post('/api/echo')
      .set('Host', HOST)
      .auth(USERNAME, PASSWORD, { type: 'basic' })
      .send({ value: 1 })
    expect(response.status).toBe(403)
  })

  it('proxies same-origin API writes with only the private operator token', async () => {
    const response = await request(application)
      .post('/api/echo')
      .set('Host', HOST)
      .set('Origin', 'https://wiki.example.test')
      .set('X-Forecast-Request', 'same-origin')
      .set('Cookie', 'browser=value')
      .auth(USERNAME, PASSWORD, { type: 'basic' })
      .send({ value: 1 })
    expect(response.status).toBe(200)
    expect(response.headers['set-cookie']).toBeUndefined()
    expect(capture).toEqual({
      authorization: 'Bearer operator-token-that-is-at-least-thirty-two-characters',
      body: JSON.stringify({ value: 1 }),
      cookie: undefined,
      path: '/api/echo',
    })
  })

  it('rejects unknown Host headers before authentication', async () => {
    const response = await request(application)
      .get('/')
      .set('Host', 'attacker.example')
      .auth(USERNAME, PASSWORD, { type: 'basic' })
    expect(response.status).toBe(421)
  })
})
