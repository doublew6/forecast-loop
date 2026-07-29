import { AlertTriangle, CloudOff, Info } from 'lucide-react'
import type { ReactNode } from 'react'

import type { Direction, RunStatus } from '../lib/types'
import { directionLabel, statusLabel } from '../lib/format'

export function PageHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description: string
  actions?: ReactNode
}) {
  return (
    <div className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

export function DemoBanner({
  dataAsOf,
  error,
  reason = 'fallback',
}: {
  dataAsOf?: string
  error?: string
  reason?: 'server' | 'fallback'
}) {
  return (
    <div className="demo-banner" role="status" data-testid="demo-banner">
      <CloudOff size={18} />
      <div>
        <strong>当前展示 Demo 数据</strong>
        <span>
          {reason === 'server'
            ? '后端已连接，但当前使用离线演示 Provider；内容不可用于投资判断。'
            : '后端接口不可用，页面已切换到内置演示数据，仅用于验证交互与布局。'}
          {dataAsOf ? ` 演示数据截面：${dataAsOf.slice(0, 10)}；不会随当前日期自动更新。` : ''}
          {error ? ` ${error}` : ''}
        </span>
      </div>
    </div>
  )
}

export function DirectionBadge({ direction, subtle = false }: { direction: Direction; subtle?: boolean }) {
  return <span className={`direction-badge ${direction}${subtle ? ' subtle' : ''}`}>{directionLabel[direction]}</span>
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`status-badge ${status}`}><i />{statusLabel[status]}</span>
}

export function QualityBadge({ quality }: { quality: 'passed' | 'warning' | 'failed' }) {
  const labels = { passed: '数据通过', warning: '数据警告', failed: '数据失败' }
  return <span className={`quality-badge ${quality}`}>{labels[quality]}</span>
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty-state">
      <Info size={22} />
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  )
}

export function SampleWarning() {
  return (
    <span className="sample-warning" title="覆盖不足 20 个预测截面">
      <AlertTriangle size={13} /> 样本不足
    </span>
  )
}

export function LoadingPanel({ size = 'page' }: { size?: 'page' | 'section' | 'inline' }) {
  return (
    <div className={`loading-panel ${size}`} role="status" aria-label="正在载入">
      <span className="visually-hidden">正在载入</span>
      <div className="loading-skeleton" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
    </div>
  )
}
