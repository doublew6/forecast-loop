import type { Direction, RunStatus } from './types'

export const directionLabel: Record<Direction, string> = {
  up: '上涨',
  neutral: '小波动',
  down: '下跌',
}

export const statusLabel: Record<RunStatus, string> = {
  completed: '已完成',
  running: '运行中',
  failed: '失败',
  pending: '待运行',
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function signedPercent(value: number, digits = 2): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(digits)}%`
}

export function formatDateTime(value: string, includeSeconds = false): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(includeSeconds ? { second: '2-digit' } : {}),
    hour12: false,
  }).format(date)
}

export function toArray<T>(value: T[] | { items?: T[] } | undefined): T[] {
  if (Array.isArray(value)) return value
  return value?.items ?? []
}
