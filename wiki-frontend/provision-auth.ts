import { randomBytes } from 'node:crypto'
import { chmod, lstat, mkdir, open, rename, unlink } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createPasswordHash } from './auth-crypto.js'
import { validatePublicOrigin, validatePublicUsername } from './server-config.js'

async function provision(): Promise<void> {
  const args = process.argv.slice(2)
  const force = args.includes('--force')
  const positional = args.filter((arg) => arg !== '--force')
  const publicOrigin = validatePublicOrigin(positional[0] ?? '')
  const username = validatePublicUsername(positional[1] ?? 'wiki-admin')
  const password = randomBytes(32).toString('base64url')
  const passwordHash = await createPasswordHash(password)
  const compiledDirectory = dirname(fileURLToPath(import.meta.url))
  const repositoryRoot = resolve(compiledDirectory, '../..')
  const privateDirectory = join(repositoryRoot, 'data/private')
  const environmentPath = join(privateDirectory, 'wiki-web.env')
  const temporaryPath = `${environmentPath}.${process.pid}.tmp`
  const contents = [
    `WIKI_PUBLIC_ORIGIN=${publicOrigin}`,
    `WIKI_USERNAME=${username}`,
    `WIKI_PASSWORD_HASH=${passwordHash}`,
    'WIKI_PORT=4174',
    '',
  ].join('\n')

  await mkdir(privateDirectory, { recursive: true, mode: 0o700 })
  let handle
  try {
    handle = await open(temporaryPath, 'wx', 0o600)
    await handle.writeFile(contents, 'utf8')
    await handle.close()
    handle = undefined
    if (!force) {
      try {
        await lstat(environmentPath)
        throw new Error('Wiki credentials already exist; pass --force to rotate them')
      } catch (error) {
        if (!(typeof error === 'object' && error && 'code' in error && error.code === 'ENOENT')) throw error
      }
    }
    await rename(temporaryPath, environmentPath)
    await chmod(environmentPath, 0o600)
    process.stdout.write(password)
  } finally {
    if (handle) await handle.close().catch(() => undefined)
    await unlink(temporaryPath).catch(() => undefined)
  }
}

provision().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : 'Credential provisioning failed'
  process.stderr.write(`${message}\n`)
  process.exitCode = 1
})
