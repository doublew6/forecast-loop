import { createHash, randomBytes, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto'

const ALGORITHM = 'scrypt'
const COST = 32768
const BLOCK_SIZE = 8
const PARALLELIZATION = 1
const KEY_LENGTH = 32
const MAX_MEMORY = 64 * 1024 * 1024

interface ParsedPasswordHash {
  salt: Buffer
  expected: Buffer
  cost: number
  blockSize: number
  parallelization: number
}

function deriveKey(password: string, salt: Buffer, keyLength: number, options: {
  N: number; r: number; p: number; maxmem: number
}): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scryptCallback(password, salt, keyLength, options, (error, derivedKey) => {
      if (error) reject(error)
      else resolve(derivedKey)
    })
  })
}

function parsePasswordHash(value: string): ParsedPasswordHash {
  const parts = value.split('$')
  if (parts.length !== 6 || parts[0] !== ALGORITHM) {
    throw new Error('WIKI_PASSWORD_HASH has an unsupported format')
  }
  const cost = Number(parts[1])
  const blockSize = Number(parts[2])
  const parallelization = Number(parts[3])
  if (cost !== COST || blockSize !== BLOCK_SIZE || parallelization !== PARALLELIZATION) {
    throw new Error('WIKI_PASSWORD_HASH uses unsupported scrypt parameters')
  }
  const salt = Buffer.from(parts[4], 'base64url')
  const expected = Buffer.from(parts[5], 'base64url')
  if (salt.length !== 16 || expected.length !== KEY_LENGTH) {
    throw new Error('WIKI_PASSWORD_HASH has invalid salt or digest length')
  }
  return { salt, expected, cost, blockSize, parallelization }
}

export function validatePasswordHash(value: string | undefined): string {
  if (!value) throw new Error('WIKI_PASSWORD_HASH is required')
  parsePasswordHash(value)
  return value
}

export async function createPasswordHash(password: string): Promise<string> {
  if (password.length < 20) throw new Error('Wiki password must contain at least 20 characters')
  const salt = randomBytes(16)
  const derived = await deriveKey(password, salt, KEY_LENGTH, {
    N: COST,
    r: BLOCK_SIZE,
    p: PARALLELIZATION,
    maxmem: MAX_MEMORY,
  })
  return [ALGORITHM, COST, BLOCK_SIZE, PARALLELIZATION, salt.toString('base64url'), derived.toString('base64url')].join('$')
}

export async function verifyPassword(password: string, encodedHash: string): Promise<boolean> {
  const parsed = parsePasswordHash(encodedHash)
  const actual = await deriveKey(password, parsed.salt, parsed.expected.length, {
    N: parsed.cost,
    r: parsed.blockSize,
    p: parsed.parallelization,
    maxmem: MAX_MEMORY,
  })
  return timingSafeEqual(actual, parsed.expected)
}

export function safeStringEqual(left: string, right: string): boolean {
  const leftDigest = createHash('sha256').update(left).digest()
  const rightDigest = createHash('sha256').update(right).digest()
  return timingSafeEqual(leftDigest, rightDigest)
}
