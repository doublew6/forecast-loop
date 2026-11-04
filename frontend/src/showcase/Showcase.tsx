import { useEffect, useRef, useState } from 'react'

import { forecastDirection, labels, metrics, parsePublication, targets } from './data'
import type { Direction, Publication, PublicForecast, Target } from './data'
import './styles.css'

const directions: Direction[] = ['up', 'neutral', 'down']
const percent = (value: number | null) => value === null ? '—' : `${(value * 100).toFixed(1)}%`
const thresholdPercent = (value: number) => new Intl.NumberFormat('zh-CN', {
  style: 'percent', maximumSignificantDigits: 3,
}).format(value)
const time = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
}).format(new Date(value))

function Distribution({ item }: { item: Pick<PublicForecast, 'probabilities'> }) {
  return <div className="distribution">
    <div className="distribution-track" aria-hidden="true">{directions.map((key) =>
      <span key={key} className={key} style={{ width: `${item.probabilities[key] * 100}%` }} />,
    )}</div>
    <dl>{directions.map((key) => <div key={key} className={key}>
      <dt><i />{labels[key]}</dt><dd>{percent(item.probabilities[key])}</dd>
    </div>)}</dl>
  </div>
}

export function Showcase() {
  const [publication, setPublication] = useState<Publication>()
  const [error, setError] = useState(false)
  const [now, setNow] = useState(Date.now)
  const [target, setTarget] = useState<Target>('csi1000-absolute-d1')
  const [lane, setLane] = useState<'formal' | 'shadow'>('formal')
  const initialized = useRef(false)
  useEffect(() => {
    let active = true
    let request: AbortController | undefined
    const load = async () => {
      request?.abort()
      request = new AbortController()
      try {
        const response = await fetch(new URL('forecast.json', document.baseURI), {
          method: 'GET', cache: 'no-store', signal: request.signal,
        })
        if (!response.ok) throw new Error('Snapshot unavailable')
        const raw = await response.text()
        if (raw.length > 1_000_000) throw new Error('Snapshot too large')
        const data = parsePublication(JSON.parse(raw))
        if (active) {
          setPublication(data); setError(false); setNow(Date.now())
          if (!initialized.current && !('status' in data)) {
            const latestD1 = [...data.forecasts].filter((item) => item.target_id === 'csi1000-absolute-d1')
              .sort((left, right) => Date.parse(right.published_at) - Date.parse(left.published_at))[0]
            if (latestD1) setLane(latestD1.lane)
            initialized.current = true
          }
        }
      } catch (cause) {
        if (active && !(cause instanceof DOMException && cause.name === 'AbortError')) setError(true)
      }
    }
    void load()
    const poll = window.setInterval(() => { void load() }, 300_000)
    const clock = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => { active = false; request?.abort(); clearInterval(poll); clearInterval(clock) }
  }, [])
  const snapshot = publication && !('status' in publication) ? publication : undefined
  const history = [...(snapshot?.forecasts ?? [])]
    .sort((left, right) => Date.parse(right.published_at) - Date.parse(left.published_at))
  const intraday = [...(snapshot?.intraday_forecasts ?? [])]
    .sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at))
  const selected = history.filter((item) => item.target_id === target && item.lane === lane)
  const latest = selected[0]
  const scores = metrics(selected)
  const stale = snapshot ? now > Date.parse(snapshot.next_update_at) : false
  const empty = publication && 'status' in publication
  return <div className="showcase">
    <header className="site-header">
      <a href="#latest" className="brand"><span className="brand-mark">F</span><span>forecast-loop<small>公开预测账本</small></span></a>
      <nav aria-label="主要导航"><a href="#latest">最新预测</a><a href="#history">历史验证</a><a href="#method">关于项目</a></nav>
      <span className="read-only"><i />只读展示</span>
    </header>
    <main>
      <section className="hero" id="latest">
        <div className="eyebrow">PREDICT · VERIFY · LEARN</div>
        <h1>让每一次预测，<br /><em>经得起回看。</em></h1>
        <p>先冻结判断，再等待结果。记录概率、核对真实表现，让预测与验证组成持续学习的闭环。</p>
        <div className="hero-meta"><span>公开记录</span><span>固定证据截面</span><span>结果独立结算</span></div>
      </section>
      {snapshot?.mode === 'demo' && <p className="demo-notice" role="note">合成数据演示：以下为测试样例，不代表真实预测或历史业绩。</p>}
      <div className={`publication-status${stale || error ? ' warning' : ''}`} role="status">
        <span className="status-light" /><div><strong>{error ? '更新暂不可用' : empty ? '等待首期公开预测' : stale ? '更新检查已超时' : snapshot?.mode === 'demo' ? '合成数据演示' : snapshot ? '公开结果已发布' : '正在读取公开预测'}</strong>
          <p>{error ? (snapshot ? '保留上次成功读取的数据；当前无法确认是否有新一期。' : '暂时无法读取公开数据，请稍后重试。')
            : empty ? '首期经过校验的预测发布后，将在这里展示。'
              : stale ? '当前保留最近一期记录，请以预测目标日为准。'
                : snapshot?.mode === 'demo' ? '以下为合成样例，不代表真实预测或历史业绩。'
                  : '公开预测保留原始封签时间，结算后补充真实结果。'}</p></div>
        {snapshot && <div className="publication-time"><span>网站数据更新 · 北京时间</span><time>{time(snapshot.generated_at)}</time></div>}
      </div>
      {!!intraday.length && <section aria-labelledby="intraday-title">
        <div className="section-heading"><div><span className="eyebrow">INTRADAY UPDATE</span><h2 id="intraday-title">盘中补发预测</h2></div><span className="section-note">保留实际生成时间</span></div>
        {intraday.map((item) => <article key={item.id} className="forecast-card">
          <div className="forecast-top"><div><span className="eyebrow">盘中补发 · 证据不完整</span><h3>中证1000 · 开盘到开盘</h3><p>{item.anchor_date} 开盘 → {item.target_date} 开盘</p></div><span className={`direction ${forecastDirection(item)}`}>{labels[forecastDirection(item)]}</span></div>
          <Distribution item={item} />
          <p className="sample-note">本预测区间已部分发生。这是一份盘中更新，不回填为盘前预测，不纳入下方历史成绩。概率已因证据缺项降低置信度。</p>
          <dl className="forecast-dates"><div><dt>预测日期</dt><dd>{item.anchor_date}</dd></div><div><dt>目标开盘日</dt><dd>{item.target_date}</dd></div><div><dt>小波动区间</dt><dd>±{thresholdPercent(item.threshold)}</dd></div></dl>
          <div className="forecast-footer"><span>证据截止 <time>{time(item.data_cutoff)}</time></span><span>实际生成 <time>{new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(item.published_at))}</time></span></div>
        </article>)}
      </section>}
      <section className="forecast-section" aria-labelledby="forecast-title">
        <div className="section-heading"><div><span className="eyebrow">01 / FORECAST</span><h2 id="forecast-title">收盘收益预测</h2></div><span className="section-note">预测日期与目标日分别记录</span></div>
        <div className="filters"><div role="group" aria-label="预测目标">{(Object.keys(targets) as Target[]).map((key) =>
          <button key={key} aria-pressed={target === key} onClick={() => { setTarget(key); if (key.endsWith('w1')) setLane('shadow') }}>{targets[key].title}</button>,
        )}</div><div role="group" aria-label="预测轨道">{(['formal', 'shadow'] as const).map((key) =>
          <button key={key} disabled={target.endsWith('w1') && key === 'formal'} aria-pressed={lane === key} onClick={() => setLane(key)}>{key === 'formal' ? '正式' : 'Shadow'}</button>,
        )}</div></div>
        {latest ? <article className="forecast-card">
          <div className="forecast-top"><div><span className="eyebrow">{latest.lane === 'formal' ? 'FORMAL' : 'SHADOW · 前瞻观察'}</span><h3>{targets[target].title}</h3><p>{targets[target].subtitle}</p></div><span className={`direction ${forecastDirection(latest)}`}>{labels[forecastDirection(latest)]}</span></div>
          <Distribution item={latest} />
          <dl className="forecast-dates"><div><dt>锚点日</dt><dd>{latest.anchor_date}</dd></div><div><dt>目标日</dt><dd>{latest.target_date}</dd></div><div><dt>小波动区间</dt><dd>±{thresholdPercent(latest.threshold)}</dd></div></dl>
          <div className="forecast-footer"><span>证据截止 <time>{time(latest.data_cutoff)}</time></span><span>预测封签 <time>{time(latest.published_at)}</time></span></div>
        </article> : <div className="empty-state"><span>—</span><h3>此轨道暂无公开预测</h3><p>正式与 Shadow 分别记录，不使用其他轨道或示例数据补齐。</p></div>}
      </section>
      <section id="history" aria-labelledby="history-title">
        <div className="section-heading"><div><span className="eyebrow">02 / TRACK RECORD</span><h2 id="history-title">预测之后，看结果</h2></div><span className="section-note">当前目标 · {lane === 'formal' ? '正式轨道' : 'Shadow 轨道'}</span></div>
        <div className="metrics"><div><span>已结算样本</span><strong>{scores.count}<small>期</small></strong></div><div><span>三分类方向命中率</span><strong>{percent(scores.accuracy)}</strong></div><div><span>平均 Brier 分数</span><strong>{scores.brier?.toFixed(3) ?? '—'}</strong><small>越低越好 · 三分类评分</small></div></div>
        <p className="sample-note">{scores.count < 20 ? '样本不足 20 期，当前表现仅供观察。' : '统计仅覆盖本页公开的已结算样本。'} 正式与 Shadow、D1 与 W1 分开计算，不合并为总成绩。</p>
        <div className="history-table"><table><caption className="sr-only">当前预测目标与轨道的公开历史记录</caption><thead><tr><th>锚点日 / 目标日</th><th>预测</th><th>实际收益</th><th>结算</th><th>Brier</th></tr></thead><tbody>
          {selected.map((item) => <tr key={item.id}><td><strong>{item.anchor_date}</strong><small>→ {item.target_date}</small></td><td><span className={`direction small ${forecastDirection(item)}`}>{labels[forecastDirection(item)]}</span></td><td>{item.result ? `${item.result.actual_return > 0 ? '+' : ''}${percent(item.result.actual_return)}` : '—'}</td><td>{item.result ? `${labels[item.result.actual_label]} · ${item.result.direction_correct ? '命中' : '未命中'}` : '待结算'}</td><td>{item.result?.brier_score.toFixed(3) ?? '—'}</td></tr>)}
          {!selected.length && <tr><td colSpan={5} className="table-empty">暂无公开记录</td></tr>}
        </tbody></table></div>
        <p className="table-footnote">最多展示最近 400 条公开预测。未结算记录不计入成绩，小波动同样参与三分类评价。</p>
      </section>
      <section id="method" className="method" aria-labelledby="method-title"><div><span className="eyebrow">03 / WHY FORECAST-LOOP</span><h2 id="method-title">结论会变，<br />判断的来路应当可验证。</h2><p>forecast-loop 是开源的预测、证据与审计框架。这个公开账本展示已封签的预测和确定性评分，帮助观察预测系统如何随时间接受检验。</p><a className="source-link" href="https://github.com/doublew6/forecast-loop" target="_blank" rel="noreferrer">查看开源项目 ↗</a></div><ol><li><span>01</span><div><h3>先固定证据与时间</h3><p>每期记录证据截止、预测封签及目标日期，历史预测保持原样。</p></div></li><li><span>02</span><div><h3>用概率表达不确定性</h3><p>展示上涨、小波动与下跌的完整分布，避免把判断简化成确定承诺。</p></div></li><li><span>03</span><div><h3>结果到期，再独立评分</h3><p>按既定阈值结算方向和 Brier 分数，不用后来的信息改写原预测。</p></div></li></ol></section>
    </main>
    <footer><span>forecast-loop <small>预测 · 验证 · 学习</small></span><p>研究展示，不构成投资建议。Shadow 为观察轨道；历史表现不保证未来结果。</p><span>只读公开版</span></footer>
  </div>
}
