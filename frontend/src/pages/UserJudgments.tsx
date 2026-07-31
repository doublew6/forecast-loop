import {
  BookOpenCheck,
  Check,
  Clock3,
  ExternalLink,
  FileKey2,
  History,
  LockKeyhole,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useIsMutating } from '@tanstack/react-query'
import { useState } from 'react'

import { DemoBanner, DirectionBadge, EmptyState, LoadingPanel, PageHeading } from '../components/Common'
import { ForecastTargetSelector } from '../components/ForecastTargetSelector'
import { useCreateUserJudgment, useUserJudgments, useUserJudgmentTargets } from '../lib/api'
import { formatDateTime, percent } from '../lib/format'
import type {
  Horizon,
  PredictionDirection,
  UserJudgment,
  UserJudgmentTarget,
} from '../lib/types'

const EMPTY_TARGETS: UserJudgmentTarget[] = []

function JudgmentSeal({ judgment }: { judgment: UserJudgment }) {
  return (
    <article className="judgment-seal" aria-live="polite">
      <div className="judgment-seal-band">
        <span><LockKeyhole size={14} /> Decision seal · 已冻结</span>
        <span>{judgment.formal_score_eligible ? '影子成绩有效' : '仅作个人存档'}</span>
      </div>
      <div className="judgment-seal-summary">
        <div>
          <span>{judgment.index_name} · {judgment.horizon}</span>
          <div><DirectionBadge direction={judgment.direction} /><strong>{percent(judgment.confidence)}</strong></div>
          <small>你的方向与主观置信度</small>
        </div>
        <div className="judgment-reveal">
          <span>封签后揭示委员会判断</span>
          <div><DirectionBadge direction={judgment.committee_direction} subtle /><strong>{judgment.committee_agreement ? '同向' : '分歧'}</strong></div>
          <small>用户判断不会改变 CIO 结论</small>
        </div>
      </div>
      <div className="judgment-seal-copy">
        <section><span>核心理由</span><p>{judgment.rationale}</p></section>
        <section><span>最强反方证据</span><p>{judgment.counter_evidence}</p></section>
        <section><span>失效条件</span><p>{judgment.invalidation_condition}</p></section>
      </div>
      {judgment.evaluation && (
        <div className="judgment-outcome">
          <Check size={15} />
          <span>
            到期结果 {judgment.evaluation.actual_label === 'up' ? '上涨' : judgment.evaluation.actual_label === 'down' ? '下跌' : '小波动'}
            {' · '}{percent(judgment.evaluation.actual_return, 2)}
            {' · '}{judgment.evaluation.sign_correct ? '符号命中' : '符号未命中'}
          </span>
        </div>
      )}
      <footer className="judgment-seal-footer">
        <div><span>冻结时间</span><strong>{formatDateTime(judgment.submitted_at)}</strong></div>
        <div><span>判断哈希</span><code title={judgment.content_hash}>{judgment.content_hash.slice(0, 14)}…</code></div>
        <a href={judgment.wiki_url} target="_blank" rel="noreferrer">
          <BookOpenCheck size={14} /> 查看 Wiki 快照 <ExternalLink size={12} />
        </a>
      </footer>
    </article>
  )
}

function JudgmentForm({
  target,
  frozen,
}: {
  target: UserJudgmentTarget
  frozen?: UserJudgment
}) {
  const create = useCreateUserJudgment()
  const locked = !target.submission_open || create.isPending
  const [direction, setDirection] = useState<PredictionDirection | ''>('')
  const [confidence, setConfidence] = useState(0.6)
  const [rationale, setRationale] = useState('')
  const [counterEvidence, setCounterEvidence] = useState('')
  const [invalidation, setInvalidation] = useState('')
  const [blindAttestation, setBlindAttestation] = useState(false)
  const sealed = frozen ?? create.data

  if (sealed) return <JudgmentSeal judgment={sealed} />

  const ready = Boolean(
    direction
    && rationale.trim().length >= 20
    && counterEvidence.trim().length >= 10
    && invalidation.trim().length >= 10,
  )

  return (
    <form
      className="judgment-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (!direction || !ready || !target.submission_open) return
        create.mutate({
          forecast_id: target.forecast_id,
          direction,
          confidence,
          rationale: rationale.trim(),
          counter_evidence: counterEvidence.trim(),
          invalidation_condition: invalidation.trim(),
          blind_attestation: blindAttestation,
        })
      }}
    >
      {!target.submission_open && (
        <div className="action-message error" role="alert">{target.submission_note}</div>
      )}
      {create.isError && (
        <div className="action-message error" role="alert">
          冻结失败：{create.error.message}。输入仍保留，可以核对后重试。
        </div>
      )}

      <fieldset className="direction-fieldset" disabled={locked}>
        <legend>1. 先选方向</legend>
        <p>只允许上涨或下跌；委员会结论会在封签后揭示。</p>
        <div className="direction-choice-grid">
          <label className={direction === 'up' ? 'selected up' : 'up'}>
            <input
              type="radio"
              name="direction"
              value="up"
              checked={direction === 'up'}
              onChange={() => setDirection('up')}
            />
            <TrendingUp size={21} />
            <span><strong>上涨</strong><small>目标日收盘方向向上</small></span>
          </label>
          <label className={direction === 'down' ? 'selected down' : 'down'}>
            <input
              type="radio"
              name="direction"
              value="down"
              checked={direction === 'down'}
              onChange={() => setDirection('down')}
            />
            <TrendingDown size={21} />
            <span><strong>下跌</strong><small>目标日收盘方向向下</small></span>
          </label>
        </div>
      </fieldset>

      <div className="judgment-confidence">
        <div><label htmlFor="judgment-confidence">主观置信度</label><strong>{percent(confidence)}</strong></div>
        <input
          id="judgment-confidence"
          type="range"
          min="0.5"
          max="1"
          step="0.01"
          value={confidence}
          disabled={locked}
          onChange={(event) => setConfidence(Number(event.target.value))}
        />
        <small>这里只记录二元主观强度，不伪装成三分类概率或 Brier。</small>
      </div>

      <label className="judgment-text-field" htmlFor="judgment-rationale">
        <span><strong>2. 我的核心理由</strong><small>{rationale.trim().length}/4000 · 至少 20 字</small></span>
        <textarea
          id="judgment-rationale"
          required
          minLength={20}
          maxLength={4000}
          disabled={locked}
          value={rationale}
          onChange={(event) => setRationale(event.target.value)}
          placeholder="写出因果链：什么事实，通过什么传导，会让这个指数在该周期上涨或下跌？"
        />
      </label>

      <div className="judgment-reason-grid">
        <label className="judgment-text-field" htmlFor="judgment-counter">
          <span><strong>3. 最强反方证据</strong><small>{counterEvidence.trim().length}/2000 · 至少 10 字</small></span>
          <textarea
            id="judgment-counter"
            required
            minLength={10}
            maxLength={2000}
            disabled={locked}
            value={counterEvidence}
            onChange={(event) => setCounterEvidence(event.target.value)}
            placeholder="哪条证据最可能推翻你的判断？"
          />
        </label>
        <label className="judgment-text-field" htmlFor="judgment-invalidation">
          <span><strong>4. 什么情况会证明我错了</strong><small>{invalidation.trim().length}/2000 · 至少 10 字</small></span>
          <textarea
            id="judgment-invalidation"
            required
            minLength={10}
            maxLength={2000}
            disabled={locked}
            value={invalidation}
            onChange={(event) => setInvalidation(event.target.value)}
            placeholder="给出可以观察、可以触发的失效条件。"
          />
        </label>
      </div>

      <label className="blind-attestation">
        <input
          type="checkbox"
          disabled={locked}
          checked={blindAttestation}
          onChange={(event) => setBlindAttestation(event.target.checked)}
        />
        <ShieldCheck size={18} />
        <span>
          <strong>我尚未查看本次委员会结论</strong>
          <small>
            {target.mode === 'live'
              ? '勾选后才有资格进入用户影子成绩；这是自我声明，不是密码学证明。'
              : 'Demo 永不计分；此项只用于练习完整流程。'}
          </small>
        </span>
      </label>

      <div className="judgment-submit-row">
        <p>
          <LockKeyhole size={14} />
          冻结后不可 PATCH、不可覆盖；每日观点不会自动成为正式 Wiki 真理。
        </p>
        <button
          className="primary-button judgment-submit"
          type="submit"
          disabled={!ready || !target.submission_open || create.isPending}
        >
          <LockKeyhole size={15} />
          {create.isPending ? '正在封签…' : '冻结我的判断'}
        </button>
      </div>
    </form>
  )
}

export function UserJudgments() {
  const targetsQuery = useUserJudgmentTargets()
  const historyQuery = useUserJudgments()
  const judgmentPending = useIsMutating({
    mutationKey: ['user-judgments', 'create'],
  }) > 0
  const [indexCode, setIndexCode] = useState('')
  const [horizon, setHorizon] = useState<Horizon>('D1')
  const targets = targetsQuery.data?.data ?? EMPTY_TARGETS
  const history = historyQuery.data?.data ?? []
  const instruments = [...new Map(
    targets.map((item) => [
      item.index_code,
      { code: item.index_code, name: item.index_name },
    ]),
  ).values()]
  const availableIndexCodes = new Set(targets.map((item) => item.index_code))
  const availableTargets = new Set(
    targets.map((item) => `${item.index_code}:${item.horizon}`),
  )
  const availableHorizons = (['D1', 'D2'] as Horizon[]).filter(
    (item) => targets.some((target) => target.horizon === item),
  )
  const selected = targets.find(
    (item) => item.index_code === indexCode && item.horizon === horizon,
  ) ?? targets[0]
  const selectedIndexCode = selected?.index_code ?? indexCode
  const selectedHorizon = selected?.horizon ?? horizon
  const frozen = selected
    ? history.find((item) => item.forecast_id === selected.forecast_id)
    : undefined

  if (targetsQuery.isLoading) return <LoadingPanel />

  return (
    <div className="page judgment-page">
      <PageHeading
        eyebrow="独立输入 Agent"
        title="我的独立判断"
        description="先判断，再解释，再封签。系统随后才显示委员会方向，并用同一市场结果检验你的长期表现。"
        actions={
          <div className="judgment-heading-status">
            <FileKey2 size={17} />
            <div><span>影子席位</span><strong>不影响 CIO · 权重 0</strong></div>
          </div>
        }
      />

      {targetsQuery.data?.mode === 'demo' && (
        <DemoBanner
          reason={targetsQuery.data.demo_reason}
          error={targetsQuery.data.error}
        />
      )}
      {targetsQuery.isError && (
        <>
          <div className="action-message error" role="alert">
            无法读取可封签目标：{targetsQuery.error.message}
          </div>
          <EmptyState title="用户判断入口不可用" description="该页面不会用静态 Demo 假装保存成功，请先连接本地 API。" />
        </>
      )}

      {!targetsQuery.isError && targets.length > 0 && (
        <>
          <ForecastTargetSelector
            instruments={instruments}
            indexCode={selectedIndexCode}
            horizon={selectedHorizon}
            horizons={availableHorizons}
            onIndexChange={(nextIndexCode) => {
              setIndexCode(nextIndexCode)
              if (!availableTargets.has(`${nextIndexCode}:${selectedHorizon}`)) {
                const fallback = targets.find(
                  (item) => item.index_code === nextIndexCode,
                )
                if (fallback) setHorizon(fallback.horizon)
              }
            }}
            onHorizonChange={(nextHorizon) => {
              setIndexCode(selectedIndexCode)
              setHorizon(nextHorizon)
            }}
            availableIndexCodes={availableIndexCodes}
            availableTargets={availableTargets}
            disabled={judgmentPending}
          />

          {selected ? (
            <section className="judgment-workbench">
              <div className="panel judgment-form-panel">
                <div className="judgment-target-context">
                  <div><span>绑定运行</span><code title={selected.run_id}>{selected.run_id.slice(0, 16)}…</code></div>
                  <div><span>数据截止</span><strong>{formatDateTime(selected.data_cutoff)}</strong></div>
                  <div><span>目标交易日</span><strong>{selected.target_date}</strong></div>
                  <div>
                    <span>封签状态</span>
                    <strong className={selected.submission_open ? 'open' : 'closed'}>
                      {frozen ? '已冻结' : selected.submission_open ? '窗口开放' : '已关闭'}
                    </strong>
                  </div>
                </div>
                <JudgmentForm
                  key={selected.forecast_id}
                  target={selected}
                  frozen={frozen}
                />
              </div>

              <aside className="judgment-audit-rail">
                <div className="panel judgment-audit-panel">
                  <div className="panel-heading"><div><span className="eyebrow">审计清单</span><h2>封签规则</h2></div></div>
                  <ol>
                    <li className="done"><Check size={13} /><span><strong>只读目标</strong>不会提前返回 CIO 方向</span></li>
                    <li className={frozen ? 'done' : ''}><Check size={13} /><span><strong>强制理由</strong>因果、反证、失效条件</span></li>
                    <li className={frozen ? 'done' : ''}><Check size={13} /><span><strong>不可覆盖</strong>同一用户×Forecast 仅一份</span></li>
                    <li className={frozen ? 'done' : ''}><Check size={13} /><span><strong>Wiki 封条</strong>内容与文件双 SHA-256</span></li>
                  </ol>
                </div>
                <div className="panel judgment-window-panel">
                  <Clock3 size={18} />
                  <div>
                    <span>判断窗口</span>
                    <strong>{selected.submission_deadline ? formatDateTime(selected.submission_deadline) : 'Demo 不设正式截止'}</strong>
                    <p>{selected.submission_note}</p>
                  </div>
                </div>
                <div className="judgment-boundary-note">
                  <ShieldCheck size={17} />
                  <p><strong>知识边界</strong>每日判断保存在私有 User Judgment Wiki，不进入可被 Agent 引用的正式 Agent Wiki；复盘后只能另行提出 Lesson。</p>
                </div>
              </aside>
            </section>
          ) : <EmptyState title="该目标暂不可用" description="请选择有最新预测的指数和周期。" />}
        </>
      )}

      {!targetsQuery.isError && targets.length === 0 && (
        <EmptyState title="没有可绑定的预测" description="完成一次投委会运行后，系统才会提供不含结论的盲判目标。" />
      )}

      <section className="panel judgment-history">
        <div className="panel-heading">
          <div><span className="eyebrow">判断记录</span><h2>我的封签账本</h2></div>
          <span><History size={14} /> {history.length} 份记录</span>
        </div>
        {historyQuery.isError ? (
          <div className="action-message error" role="alert">
            无法读取封签账本：{historyQuery.error.message}。已有记录不会被当成空账本。
          </div>
        ) : historyQuery.isLoading ? <LoadingPanel size="section" /> : history.length ? (
          <div className="judgment-history-list">
            {history.map((item) => (
              <article key={item.id}>
                <div><span>{item.target_date}</span><strong>{item.index_name} · {item.horizon}</strong></div>
                <div className="judgment-history-direction">
                  <DirectionBadge direction={item.direction} subtle />
                </div>
                <p>{item.rationale}</p>
                <span className={item.formal_score_eligible ? 'eligible' : 'practice'}>
                  {item.formal_score_eligible ? '影子计分' : item.mode === 'demo' ? 'Demo 练习' : '非盲判存档'}
                </span>
                <code title={item.content_hash}>{item.content_hash.slice(0, 10)}…</code>
                <a href={item.wiki_url} target="_blank" rel="noreferrer" aria-label={`查看 ${item.index_name} ${item.horizon} 的 Wiki 快照`}>
                  <ExternalLink size={14} />
                </a>
              </article>
            ))}
          </div>
        ) : <EmptyState title="还没有个人判断" description="完成第一份盲判后，这里会成为你的不可覆盖预测账本。" />}
      </section>
    </div>
  )
}
