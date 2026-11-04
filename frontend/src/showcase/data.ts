export type Direction = 'up' | 'neutral' | 'down'
export type Target = 'csi1000-absolute-d1' | 'csi1000-vs-csi300-relative-w1'
export interface PublicForecast {
  id: string
  target_id: Target
  lane: 'formal' | 'shadow'
  anchor_date: string
  target_date: string
  data_cutoff: string
  published_at: string
  probabilities: Record<Direction, number>
  threshold: number
  result: {
    actual_return: number
    actual_label: Direction
    brier_score: number
    direction_correct: boolean
  } | null
}
export interface PublicIntradayForecast {
  id: string
  request_hash: string
  target_id: 'csi1000-open-to-open-d1'
  publication_kind: 'intraday_supplement'
  performance_included: false
  evidence_status: 'partial'
  anchor_date: string
  target_date: string
  data_cutoff: string
  published_at: string
  probabilities: Record<Direction, number>
  threshold: number
}
export interface PublicSnapshot {
  schema_version: 'forecast-loop.public-showcase/v1' | 'forecast-loop.public-showcase/v2'
  intraday_forecasts?: PublicIntradayForecast[]
  generated_at: string
  next_update_at: string
  mode: 'live' | 'demo'
  forecasts: PublicForecast[]
}
export interface EmptyPublication {
  schema_version: 'forecast-loop.public-showcase/v1'
  status: 'awaiting_publication'
}
export type Publication = PublicSnapshot | EmptyPublication
export const labels: Record<Direction, string> = { up: '上涨', neutral: '小波动', down: '下跌' }
export const targets: Record<Target, { title: string; subtitle: string }> = {
  'csi1000-absolute-d1': { title: '中证1000 · D1', subtitle: '下一交易日收盘收益 · 绝对方向' },
  'csi1000-vs-csi300-relative-w1': { title: '中证1000 / 沪深300 · W1', subtitle: '未来五个交易日 · 相对强弱' },
}

function object(value: unknown, fields: string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== fields.length
    || fields.some((key) => !Object.hasOwn(value, key))) throw new Error('公开数据格式无效')
  return value as Record<string, unknown>
}
function number(value: unknown, minimum = -Infinity, maximum = Infinity): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error('公开数据数值无效')
  }
}
function timestamp(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !/T.*(?:Z|[+-]\d\d:\d\d)$/.test(value)
    || !Number.isFinite(Date.parse(value))) throw new Error('公开时间无效')
}
function sessionDate(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)
    || !Number.isFinite(Date.parse(value))
    || new Date(value).toISOString().slice(0, 10) !== value) throw new Error('预测日期无效')
}
function direction(value: unknown): asserts value is Direction {
  if (value !== 'up' && value !== 'neutral' && value !== 'down') throw new Error('预测方向无效')
}
export function parsePublication(value: unknown): Publication {
  if (value && typeof value === 'object' && 'status' in value) {
    const empty = object(value, ['schema_version', 'status'])
    if (empty.schema_version !== 'forecast-loop.public-showcase/v1' || empty.status !== 'awaiting_publication') {
      throw new Error('公开数据版本无效')
    }
    return value as EmptyPublication
  }
  const v2 = !!value && typeof value === 'object' && 'schema_version' in value
    && value.schema_version === 'forecast-loop.public-showcase/v2'
  const root = object(value, ['schema_version', 'generated_at', 'next_update_at', 'mode', 'forecasts',
    ...(v2 ? ['intraday_forecasts'] : [])])
  if (!['forecast-loop.public-showcase/v1', 'forecast-loop.public-showcase/v2'].includes(String(root.schema_version)) || !['live', 'demo'].includes(String(root.mode))) {
    throw new Error('公开数据版本无效')
  }
  timestamp(root.generated_at)
  timestamp(root.next_update_at)
  if (!Array.isArray(root.forecasts) || !root.forecasts.length || root.forecasts.length > 400) {
    throw new Error('公开预测列表无效')
  }
  const ids = new Set<string>()
  const episodes = new Set<string>()
  let latest = -Infinity
  for (const value of root.forecasts) {
    const item = object(value, ['id', 'target_id', 'lane', 'anchor_date', 'target_date', 'data_cutoff',
      'published_at', 'probabilities', 'threshold', 'result'])
    if (typeof item.id !== 'string' || !/^[0-9a-f]{64}$/.test(item.id) || ids.has(item.id)
      || typeof item.target_id !== 'string' || !Object.hasOwn(targets, item.target_id)
      || !['formal', 'shadow'].includes(String(item.lane))
      || (item.target_id.endsWith('w1') && item.lane !== 'shadow')) throw new Error('预测身份无效')
    ids.add(item.id)
    sessionDate(item.anchor_date)
    sessionDate(item.target_date)
    const episode = `${item.target_id}:${item.anchor_date}`
    if (episodes.has(episode)) throw new Error('重复预测截面')
    episodes.add(episode)
    timestamp(item.data_cutoff)
    timestamp(item.published_at)
    latest = Math.max(latest, Date.parse(item.published_at))
    if (item.anchor_date >= item.target_date || Date.parse(item.data_cutoff) > Date.parse(item.published_at)
      || Date.parse(item.published_at) > Date.parse(root.generated_at)) throw new Error('预测时间顺序无效')
    const probabilities = object(item.probabilities, ['up', 'neutral', 'down'])
    number(probabilities.up, 0, 1)
    number(probabilities.neutral, 0, 1)
    number(probabilities.down, 0, 1)
    if (Math.abs(probabilities.up + probabilities.neutral + probabilities.down - 1) > 1e-6) {
      throw new Error('概率分布无效')
    }
    const maximum = Math.max(probabilities.up, probabilities.neutral, probabilities.down)
    const winners = (['up', 'neutral', 'down'] as Direction[])
      .filter((key) => Math.abs((probabilities[key] as number) - maximum) <= 1e-9)
    if (winners.length !== 1) throw new Error('预测类别无唯一最大概率')
    number(item.threshold, 0)
    if (item.result !== null) {
      const result = object(item.result, ['actual_return', 'actual_label', 'brier_score', 'direction_correct'])
      number(result.actual_return)
      number(result.brier_score, 0, 2 / 3)
      direction(result.actual_label)
      if (typeof result.direction_correct !== 'boolean') throw new Error('结算结果无效')
      const actual = result.actual_return > item.threshold ? 'up'
        : result.actual_return < -item.threshold ? 'down' : 'neutral'
      const brier = (['up', 'neutral', 'down'] as Direction[])
        .reduce((sum, key) => sum + ((probabilities[key] as number) - (actual === key ? 1 : 0)) ** 2, 0) / 3
      if (result.actual_label !== actual || result.direction_correct !== (winners[0] === actual)
        || Math.abs(result.brier_score - brier) > 1e-9) throw new Error('结算结果无法复算')
    }
  }
  if (v2) {
    if (!Array.isArray(root.intraday_forecasts) || root.intraday_forecasts.length > 100) {
      throw new Error('盘中补发列表无效')
    }
    for (const row of root.intraday_forecasts) {
      const item = object(row, ['id', 'request_hash', 'target_id', 'publication_kind',
        'performance_included', 'evidence_status', 'anchor_date', 'target_date',
        'data_cutoff', 'published_at', 'probabilities', 'threshold'])
      if (typeof item.id !== 'string' || !/^[0-9a-f]{64}$/.test(item.id) || ids.has(item.id)
        || typeof item.request_hash !== 'string' || !/^[0-9a-f]{64}$/.test(item.request_hash)
        || item.target_id !== 'csi1000-open-to-open-d1'
        || item.publication_kind !== 'intraday_supplement' || item.performance_included !== false
        || item.evidence_status !== 'partial') throw new Error('盘中补发身份无效')
      ids.add(item.id)
      sessionDate(item.anchor_date); sessionDate(item.target_date)
      timestamp(item.data_cutoff); timestamp(item.published_at)
      const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai',
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
        second: '2-digit', hour12: false }).format(new Date(item.published_at)).split(' ')
      if (item.anchor_date >= item.target_date || parts[0] !== item.anchor_date
        || parts[1] <= '09:30:00' || parts[1] >= '15:00:00'
        || Date.parse(item.data_cutoff) > Date.parse(item.published_at)
        || Date.parse(item.published_at) > Date.parse(root.generated_at)) throw new Error('盘中补发时间无效')
      const p = object(item.probabilities, ['up', 'neutral', 'down'])
      number(p.up, 0, 1); number(p.neutral, 0, 1); number(p.down, 0, 1)
      if (Math.abs(p.up + p.neutral + p.down - 1) > 1e-6
        || [p.up, p.neutral, p.down].filter((v) => v === Math.max(p.up as number, p.neutral as number, p.down as number)).length !== 1) {
        throw new Error('盘中补发概率无效')
      }
      number(item.threshold, 0)
      latest = Math.max(latest, Date.parse(item.published_at))
    }
  }
  if (Date.parse(root.next_update_at) <= latest) throw new Error('更新时效无效')
  return value as PublicSnapshot
}
export function forecastDirection(item: Pick<PublicForecast, 'probabilities'>): Direction {
  return (['up', 'neutral', 'down'] as Direction[])
    .reduce((best, key) => item.probabilities[key] > item.probabilities[best] ? key : best, 'up')
}
export function metrics(items: PublicForecast[]) {
  const results = items.flatMap((item) => item.result ? [item.result] : [])
  return {
    count: results.length,
    accuracy: results.length ? results.filter((result) => result.direction_correct).length / results.length : null,
    brier: results.length ? results.reduce((sum, result) => sum + result.brier_score, 0) / results.length : null,
  }
}
