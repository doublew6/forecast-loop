import { AlertCircle, Check, Clock3, DatabaseZap, Play, RefreshCw, RotateCcw } from 'lucide-react'

import { DemoBanner, EmptyState, LoadingPanel, PageHeading, QualityBadge, StatusBadge } from '../components/Common'
import { useCreateRun, useRuns } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { RunSummary, TaskStatus, WorkflowTask } from '../lib/types'

const taskStatusLabel: Record<TaskStatus, string> = {
  queued: '排队中',
  running: '执行中',
  retry_wait: '等待重试',
  completed: '已完成',
  failed: '已失败',
}

function TaskStatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`task-status-badge ${status}`}><i />{taskStatusLabel[status]}</span>
}

function TaskRail({ task }: { task: WorkflowTask }) {
  const executing = task.status === 'running' || task.status === 'retry_wait'
  const published = task.status === 'completed'
  const failed = task.status === 'failed'
  const retryAt = task.status === 'retry_wait'
    ? `下次可执行：${formatDateTime(task.available_at, true)}`
    : null

  return (
    <div className={`task-rail ${task.status}`} aria-label={`持久任务：${taskStatusLabel[task.status]}`}>
      <div className="task-rail-steps">
        <span className="done"><i />输入已冻结</span>
        <span className={executing ? 'active' : published ? 'done' : failed ? 'failed' : ''}><i />{task.stage === 'prepared' ? '等待 worker' : '执行预测'}</span>
        <span className={published ? 'done' : failed ? 'failed' : ''}><i />结果落库</span>
      </div>
      <small>尝试 {task.attempt_count}/{task.max_attempts}{retryAt ? ` · ${retryAt}` : ''}</small>
    </div>
  )
}

function RunStateIcon({ run }: { run: RunSummary }) {
  const state = run.task?.status ?? run.status
  if (state === 'failed') return <AlertCircle size={18} />
  if (state === 'running' || state === 'retry_wait') return <RefreshCw size={18} />
  if (state === 'queued' || state === 'pending') return <Clock3 size={18} />
  return <Check size={18} />
}

export function Runs() {
  const query = useRuns()
  const createRun = useCreateRun()
  const runs = query.data?.data ?? []
  const demoMode = query.data?.mode === 'demo'
  const apiUnavailable = query.data?.demo_reason === 'fallback'

  if (query.isLoading) return <LoadingPanel />

  const actionMessage = createRun.isError
    ? `启动失败：${createRun.error.message}`
    : createRun.isSuccess
      ? '新运行已创建，列表将自动刷新。'
      : undefined

  return (
    <div className="page runs-page">
      <PageHeading
        eyebrow="任务执行"
        title="运行与数据质量"
        description="每次重跑产生新版本，不覆盖已经冻结的历史决策。"
        actions={
          <div className="run-actions">
            <div className="disabled-action">
              <button className="secondary-button" disabled aria-describedby="evaluation-import-note"><RefreshCw size={16} /> 运行到期评分</button>
              <small id="evaluation-import-note">需先导入到期行情，当前不会提交空评分。</small>
            </div>
            <button className="primary-button" disabled={createRun.isPending || apiUnavailable} onClick={() => createRun.mutate()}><Play size={16} /> 新建投委会运行</button>
          </div>
        }
      />
      {demoMode && <DemoBanner error={query.data?.error} reason={query.data?.demo_reason} />}
      {actionMessage && <div className={`action-message${createRun.isError ? ' error' : ''}`}>{actionMessage}</div>}

      <section className="run-overview metric-grid three">
        <div className="metric-card"><div className="metric-icon"><Check size={19} /></div><span>成功运行</span><strong>{runs.filter((run) => run.status === 'completed').length}</strong><small>当前列表范围</small></div>
        <div className="metric-card"><div className="metric-icon"><Clock3 size={19} /></div><span>最近耗时</span><strong>{runs.find((run) => run.status === 'completed')?.duration_seconds ?? '—'}<em> 秒</em></strong><small>从数据冻结到决策落库</small></div>
        <div className="metric-card"><div className="metric-icon"><DatabaseZap size={19} /></div><span>数据异常</span><strong>{runs.filter((run) => run.data_quality !== 'passed').length}</strong><small>警告与失败均保留审计记录</small></div>
      </section>

      <section className="panel runs-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">执行历史</span><h2>运行记录</h2></div>
          <button className="icon-text-button" onClick={() => query.refetch()} disabled={query.isFetching}><RotateCcw className={query.isFetching ? 'spinning' : ''} size={15} /> 刷新</button>
        </div>
        <div className="run-list">
          {runs.length === 0 && (
            <EmptyState
              title="还没有运行记录"
              description="创建一次运行后，排队、重试和结果落库状态会显示在这里。"
            />
          )}
          {runs.map((run) => (
            <article className="run-row" key={run.id}>
              <div className={`run-state-icon ${run.task?.status ?? run.status}`}>
                <RunStateIcon run={run} />
              </div>
              <div className="run-identity"><strong>{run.id}</strong><span>{formatDateTime(run.as_of, true)}</span></div>
              <div className="run-field"><span>任务状态</span>{run.task ? <TaskStatusBadge status={run.task.status} /> : <StatusBadge status={run.status} />}</div>
              <div className="run-field"><span>数据质量</span><QualityBadge quality={run.data_quality} /></div>
              <div className="run-field"><span>输出</span><strong>{run.forecasts_count !== undefined ? `${run.forecasts_count} 个预测` : '未返回统计'}</strong></div>
              <div className="run-field"><span>耗时</span><strong>{run.duration_seconds ? `${run.duration_seconds} 秒` : '—'}</strong></div>
              {run.task && <TaskRail task={run.task} />}
              {run.error && <div className="run-error"><AlertCircle size={14} />{run.error}</div>}
              {run.task?.last_error && run.task.last_error !== run.error && (
                <div className={`run-error task-error ${run.task.status}`}><AlertCircle size={14} />{run.task.last_error}</div>
              )}
            </article>
          ))}
        </div>
      </section>

      <div className="run-policy-note">
        <AlertCircle size={17} />
        <div><strong>失败即停止发布</strong><span>关键行情缺失、时间戳异常或引用无法冻结时，本次运行保留失败记录，不会沿用旧数据生成新预测。</span></div>
      </div>
    </div>
  )
}
