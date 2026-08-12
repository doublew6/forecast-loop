import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  FlaskConical,
  GitBranch,
  Play,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'

import { EmptyState, LoadingPanel, PageHeading } from '../components/Common'
import {
  useAgentBadCases,
  useAgentEvalExperiments,
  useAgentEvalSuites,
  useCreateAgentEvalExperiment,
  useTransitionAgentBadCase,
} from '../lib/api'
import { formatDateTime, percent } from '../lib/format'
import type {
  AgentBadCase,
  AgentBadCaseStatus,
  AgentEvalDecision,
  AgentEvalExperiment,
} from '../lib/types'

const decisionLabel: Record<AgentEvalDecision, string> = {
  pending: '等待评测',
  pass: '允许放行',
  fail: '阻止放行',
  insufficient_sample: '样本不足',
}

const experimentStatusLabel: Record<AgentEvalExperiment['status'], string> = {
  queued: '等待准备',
  awaiting_draft: '等待 Codex 草稿',
  ready_to_finalize: '等待确定性 Finalize',
  running: '正在评测',
  completed: '已完成',
  failed: '失败',
}

const badCaseLabel: Record<AgentBadCaseStatus, string> = {
  detected: '已发现',
  triaged: '已分诊',
  confirmed: '已确认',
  materialized: '已回流',
  resolved: '已解决',
  rejected: '已驳回',
}

function DecisionBadge({ decision }: { decision: AgentEvalDecision }) {
  const Icon = decision === 'pass'
    ? CheckCircle2
    : decision === 'fail'
      ? XCircle
      : AlertTriangle
  return (
    <span className={`eval-decision ${decision}`}>
      <Icon size={14} />{decisionLabel[decision]}
    </span>
  )
}

function metric(value: number | null | undefined, kind: 'delta' | 'ratio' | 'percent') {
  if (value === null || value === undefined) return '—'
  if (kind === 'ratio') return `${value.toFixed(2)}×`
  if (kind === 'percent') return percent(value)
  return `${value >= 0 ? '+' : ''}${value.toFixed(4)}`
}

function ExperimentRow({ experiment }: { experiment: AgentEvalExperiment }) {
  const waitingForDraft = experiment.status === 'awaiting_draft'
  return (
    <article className="eval-experiment-row">
      <div>
        {waitingForDraft
          ? <span className="eval-decision awaiting-draft"><AlertTriangle size={14} />{experimentStatusLabel[experiment.status]}</span>
          : <DecisionBadge decision={experiment.release_decision} />}
        <span>{formatDateTime(experiment.created_at, true)}</span>
      </div>
      <div className="eval-experiment-identity">
        <strong>{experiment.suite_id}</strong>
        <code>{experiment.id}</code>
      </div>
      <div>
        <span>对照</span>
        <strong>{experiment.baseline_target_id}</strong>
      </div>
      <ArrowRight size={15} />
      <div>
        <span>候选</span>
        <strong>{experiment.candidate_target_id}</strong>
      </div>
    </article>
  )
}

function gatePassed(value: boolean | { passed?: boolean } | undefined) {
  return typeof value === 'boolean' ? value : value?.passed
}

function TargetGateList({ experiment }: { experiment?: AgentEvalExperiment }) {
  const targets = Object.entries(experiment?.summary.targets ?? {})
  if (!targets.length) {
    return <EmptyState title="暂无按目标门禁" description="v2 replay finalize 后按 D1、W1 等目标分别展示，不跨周期合并。" />
  }
  return (
    <div className="eval-target-gate-list">
      {targets.map(([targetId, gate]) => {
        const decision = gate.decision ?? (gate.release_gate ? 'pass' : 'fail')
        const hard = gate.hard_gates ?? {}
        const hardGateValues = [hard.schema_valid, hard.cutoff_valid, hard.citation_valid, hard.trace_valid, hard.must_pass_bad_case]
        const hardPassed = hardGateValues.filter((item) => gatePassed(item) === true).length
        return (
          <article key={targetId} className="eval-target-gate-row">
            <div><DecisionBadge decision={decision} /><strong>{targetId}</strong><small>{gate.episode_count ?? 0} 个独立 episode</small></div>
            <div><span>确定性门禁</span><strong>{hardPassed}/{hardGateValues.length || 5}</strong><small>schema · cutoff · citation · trace · bad case</small></div>
            <div><span>Brier Δ</span><strong>{metric(gate.metric_gates?.brier_delta, 'delta')}</strong><small>方向下降 {percent(gate.metric_gates?.direction_drop)}</small></div>
            <div><span>耗时 / Token</span><strong>{metric(gate.metric_gates?.p95_latency_ratio, 'ratio')} · {metric(gate.metric_gates?.token_ratio, 'ratio')}</strong><small>{gate.metric_gates?.passed === false ? '性能门禁未通过' : '按冻结 policy 判断'}</small></div>
            <div><span>Ablation / 推理</span><strong>{gate.ablation?.length ?? 0} / {gate.reasoning ? '已附' : '—'}</strong><small>推理评分仅作 advisory</small></div>
          </article>
        )
      })}
    </div>
  )
}

function nextBadCaseAction(item: AgentBadCase): {
  status: Exclude<AgentBadCaseStatus, 'detected'>
  label: string
} | null {
  if (item.status === 'detected') return { status: 'triaged', label: '进入分诊' }
  if (item.status === 'triaged') return { status: 'confirmed', label: '确认回归样例' }
  if (item.status === 'confirmed') return { status: 'materialized', label: '写入离线集' }
  if (item.status === 'materialized') return { status: 'resolved', label: '标记已解决' }
  return null
}

export function Evaluations() {
  const suitesQuery = useAgentEvalSuites()
  const experimentsQuery = useAgentEvalExperiments()
  const badCasesQuery = useAgentBadCases()
  const createExperiment = useCreateAgentEvalExperiment()
  const transitionBadCase = useTransitionAgentBadCase()
  const [suiteSelection, setSuiteSelection] = useState('')
  const [baselineSelection, setBaselineSelection] = useState('')
  const [candidateSelection, setCandidateSelection] = useState('')

  const suites = suitesQuery.data
  const defaultSuite = suites?.[0]
  const suiteKey = suiteSelection || (defaultSuite
    ? `${defaultSuite.source}:${defaultSuite.suite_id}:${defaultSuite.version}`
    : '')
  const selectedSuite = suites?.find(
    (suite) => `${suite.source}:${suite.suite_id}:${suite.version}` === suiteKey,
  )
  const comparisonIds = selectedSuite?.arm_ids?.length
    ? selectedSuite.arm_ids
    : selectedSuite?.target_ids ?? []
  const fileReplay = selectedSuite?.runner_kind === 'codex_file_replay'
  const baseline = comparisonIds.includes(baselineSelection)
    ? baselineSelection
    : comparisonIds[0] ?? ''
  const candidate = comparisonIds.includes(candidateSelection)
    ? candidateSelection
    : comparisonIds[1] ?? ''

  const experiments = experimentsQuery.data ?? []
  const latestCompleted = experiments.find((item) => item.status === 'completed')
  const latestExperiment = experiments[0]
  const awaitingDraft = experiments.find((item) => item.status === 'awaiting_draft')
  const badCases = badCasesQuery.data ?? []
  const activeBadCases = badCases.filter(
    (item) => item.status !== 'resolved' && item.status !== 'rejected',
  )
  const loading = suitesQuery.isLoading || experimentsQuery.isLoading || badCasesQuery.isLoading
  if (loading) return <LoadingPanel />

  const runExperiment = () => {
    if (!selectedSuite || fileReplay || !baseline || !candidate || baseline === candidate) return
    createExperiment.mutate({
      suite_id: selectedSuite.suite_id,
      suite_version: selectedSuite.version,
      baseline_target_id: baseline,
      candidate_target_id: candidate,
      source: selectedSuite.source,
    })
  }
  const transition = (item: AgentBadCase) => {
    const action = nextBadCaseAction(item)
    if (!action) return
    const now = new Date()
    const version = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('.')
    transitionBadCase.mutate({
      id: item.id,
      input: {
        to_status: action.status,
        actor: 'web-operator',
        notes: `Operator moved the case to ${action.status}.`,
        ...(action.status === 'confirmed'
          ? {
              test_case: {
                trace_id: item.trace_id,
                input_hash: item.input_hash,
                expected_behavior: item.expected_behavior,
              },
            }
          : {}),
        ...(action.status === 'materialized'
          ? { dataset_id: 'agent-regressions', dataset_version: version }
          : {}),
      },
    })
  }

  const error = suitesQuery.error ?? experimentsQuery.error ?? badCasesQuery.error
  return (
    <div className="page evaluations-page">
      <PageHeading
        eyebrow="持续评测"
        title="Agent 版本放行台"
        description="用冻结测试集比较 baseline 与 candidate；确定性门禁负责阻断退化，定性评分只作为辅助信号。"
        actions={(
          <button
            className="icon-text-button"
            onClick={() => Promise.all([
              experimentsQuery.refetch(),
              badCasesQuery.refetch(),
            ])}
          >
            <RefreshCw size={15} />刷新
          </button>
        )}
      />

      {error && <div className="action-message error">评测服务不可用：{error.message}</div>}

      {awaitingDraft && (
        <div className="eval-awaiting-draft" role="status">
          <AlertTriangle size={17} />
          <div><strong>评测正在等待 Codex 草稿</strong><span>{awaitingDraft.id} · {[
            ...(awaitingDraft.summary.pending_arms ?? ['baseline', 'candidate']),
            ...(awaitingDraft.summary.pending_tasks ?? []),
          ].join(' / ')}；结果尚未揭示，Finalize 也尚未执行。</span></div>
        </div>
      )}

      <section className="eval-release-strip">
        <div className={`eval-release-verdict ${latestCompleted?.release_decision ?? 'pending'}`}>
          <span>最近发布结论</span>
          <strong>{decisionLabel[latestCompleted?.release_decision ?? 'pending']}</strong>
          <small>{latestCompleted ? `${latestCompleted.suite_id} · ${formatDateTime(latestCompleted.completed_at ?? latestCompleted.created_at, true)}` : '还没有完成的评测'}</small>
        </div>
        <div><span>Must-pass</span><strong>{metric(latestCompleted?.summary.must_pass_rate, 'percent')}</strong><small>必须为 100%</small></div>
        <div><span>Brier Δ</span><strong>{metric(latestCompleted?.summary.metric_gates?.brier_delta, 'delta')}</strong><small>上限 +0.0100</small></div>
        <div><span>P95 耗时</span><strong>{metric(latestCompleted?.summary.metric_gates?.p95_latency_ratio, 'ratio')}</strong><small>上限 1.20×</small></div>
        <div><span>Token</span><strong>{metric(latestCompleted?.summary.metric_gates?.token_ratio, 'ratio')}</strong><small>上限 1.15×</small></div>
      </section>

      <section className="panel eval-target-gates-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">Target gates · v2</span><h2>按目标发布门禁</h2><span className="panel-caption">每个目标单独判断样本、质量、耗时和成本；盲审不会独自放行或阻断。</span></div>
          {latestExperiment && <span className="eval-experiment-status">{experimentStatusLabel[latestExperiment.status]}</span>}
        </div>
        <TargetGateList experiment={latestCompleted ?? latestExperiment} />
      </section>

      <section className="eval-workbench">
        <div className="panel eval-launcher">
          <div className="panel-heading">
            <div><span className="eyebrow">离线 Benchmark</span><h2>新建对照实验</h2></div>
            <FlaskConical size={20} />
          </div>
          <label>
            <span>评测集</span>
            <select
              value={suiteKey}
              onChange={(event) => {
                setSuiteSelection(event.target.value)
                setBaselineSelection('')
                setCandidateSelection('')
              }}
            >
              {(suites ?? []).map((suite) => (
                <option
                  key={`${suite.source}:${suite.suite_id}:${suite.version}`}
                  value={`${suite.source}:${suite.suite_id}:${suite.version}`}
                >
                  {suite.title} · v{suite.version}
                </option>
              ))}
            </select>
          </label>
          {selectedSuite && (
            <div className="eval-suite-note">
              <ShieldCheck size={16} />
              <p><strong>{selectedSuite.case_count} 个冻结 case</strong><span>{fileReplay ? 'v2 仅允许 CLI / 文件 prepare，结果揭示与 finalize 不暴露 HTTP' : `${selectedSuite.synthetic ? '公开合成基准' : '私有回放集'} · ${selectedSuite.content_hash.slice(0, 12)}…`}</span></p>
            </div>
          )}
          <div className="eval-target-pair">
            <label><span>Baseline</span><select value={baseline} onChange={(event) => setBaselineSelection(event.target.value)}>{comparisonIds.map((id) => <option key={id}>{id}</option>)}</select></label>
            <ArrowRight size={17} />
            <label><span>Candidate</span><select value={candidate} onChange={(event) => setCandidateSelection(event.target.value)}>{comparisonIds.map((id) => <option key={id}>{id}</option>)}</select></label>
          </div>
          <button className="primary-button eval-run-button" onClick={runExperiment} disabled={!selectedSuite || fileReplay || baseline === candidate || createExperiment.isPending}>
            <Play size={15} />{fileReplay ? '请使用 agent-eval prepare' : createExperiment.isPending ? '正在入队…' : '运行评测并执行门禁'}
          </button>
          {createExperiment.isError && <div className="action-message error">{createExperiment.error.message}</div>}
        </div>

        <div className="panel eval-history">
          <div className="panel-heading">
            <div><span className="eyebrow">实验账本</span><h2>最近比较</h2></div>
            <span className="panel-caption">异步任务完成后自动刷新</span>
          </div>
          <div className="eval-experiment-list">
            {experiments.length === 0 && <EmptyState title="还没有评测实验" description="从左侧选择 suite 与两个目标版本。" />}
            {experiments.slice(0, 8).map((item) => <ExperimentRow key={item.id} experiment={item} />)}
          </div>
        </div>
      </section>

      <section className="panel bad-case-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">Bad case 回流</span><h2>待治理案例</h2><span className="panel-caption">发现 → 分诊 → 确认 → 写入离线集 → 解决</span></div>
          <strong className="queue-count">{activeBadCases.length} 个进行中</strong>
        </div>
        <div className="bad-case-list">
          {activeBadCases.length === 0 && <EmptyState title="当前没有待处理 bad case" description="确定性 evaluator 失败后会自动进入这里，也可从 trace 手动创建。" />}
          {activeBadCases.map((item) => {
            const action = nextBadCaseAction(item)
            return (
              <article key={item.id} className={`bad-case-row ${item.severity}`}>
                <div className="bad-case-severity"><AlertTriangle size={16} /><span>{item.severity}</span></div>
                <div className="bad-case-copy"><strong>{item.title}</strong><p>{item.summary}</p><code>{item.issue_type} · {item.id.slice(0, 8)}</code></div>
                <div className="bad-case-state"><span>{badCaseLabel[item.status]}</span><small>{item.dataset_id ? `${item.dataset_id}@${item.dataset_version}` : formatDateTime(item.updated_at, true)}</small></div>
                <Link className="icon-text-button" to={`/traces/${item.trace_id}`}><GitBranch size={14} />Trace</Link>
                {action && <button className="secondary-button" onClick={() => transition(item)} disabled={transitionBadCase.isPending}>{action.label}</button>}
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}
