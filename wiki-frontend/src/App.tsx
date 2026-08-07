import {
  AlertCircle, Archive, ArrowRight, BookOpenText, CheckCircle2, ChevronRight,
  Database, ExternalLink, FileCheck2, FileClock, FileText, FileUp, FolderOpen,
  History, Link2, LoaderCircle, RefreshCw, Search, ShieldCheck, Sparkles,
  UploadCloud,
} from 'lucide-react'
import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'

import {
  createChallenge, loadWorkbench, materializeFeedback, prepareWikiJob,
  syncReflectionFeedback, uploadSource,
} from './api'
import type {
  CausalStatus, FeedbackHealth, HealthStatus, IssueType, WikiEntry, WikiSource,
  WorkbenchData,
} from './types'

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

const issueLabels: Record<Exclude<IssueType, 'right_reason'>, string> = {
  factual_error: '事实错误',
  source_outdated: '来源过期',
  scope_mismatch: '适用范围不匹配',
  reasoning_failure: '推理链失效',
  transmission_mapping: '传导映射错误',
  horizon_timing: '时间周期错误',
  risk_plan_failure: '风险方案失效',
  missing_invalidation: '缺少失效条件',
  other: '其他问题',
}

interface DomainDefinition {
  id: string
  label: string
  description: string
  tags: string[]
  idTokens: string[]
}

interface DomainModel extends DomainDefinition {
  entries: WikiEntry[]
  sourceUrls: string[]
  uploadedSources: WikiSource[]
}

const DOMAIN_DEFINITIONS: DomainDefinition[] = [
  {
    id: 'index', label: '指数与风格',
    description: '指数暴露、规模风格、相对强弱与配置边界。',
    tags: ['index', 'chinext', 'csi1000', 'csi300', 'csi500', 'star50', 'style', 'allocation'],
    idTokens: ['-INDEX-'],
  },
  {
    id: 'macro', label: '宏观与政策',
    description: '货币、财政、监管与流动性变化的传导框架。',
    tags: ['macro', 'monetary-policy', 'fiscal-policy', 'regulation', 'liquidity'],
    idTokens: ['-MACRO-'],
  },
  {
    id: 'industry', label: '产业与主题',
    description: '产业链、技术周期与结构性主题研究。',
    tags: ['industry', 'technology', 'ai', 'memory', 'hbm', 'semiconductor', 'storage'],
    idTokens: ['-INDUSTRY-'],
  },
  {
    id: 'market', label: '市场事件与策略',
    description: '市场资讯、预期差、事件路径与策略映射。',
    tags: ['market-strategy', 'market-news', 'event', 'expectations', 'disclosure'],
    idTokens: ['-MARKET-'],
  },
  {
    id: 'cross-market', label: '跨市场研究',
    description: '全球权益与加密资产对 A 股状态的观察性映射。',
    tags: ['crypto', 'cross-market', 'twenty-four-seven', 'global-equity', 'single-stock'],
    idTokens: ['-CROSS-MARKET-', '-GLOBAL-EQUITY-'],
  },
  {
    id: 'method', label: '方法、证据与风险',
    description: 'D1 标签、来源分级、校准、反证与验证规范。',
    tags: ['forecast', 'labels', 'evaluation', 'calibration', 'leakage', 'validation', 'sources', 'provenance', 'evidence', 'risk'],
    idTokens: ['-PREDICTION-', '-SOURCE-', '-RISK-'],
  },
]

const SOURCE_NAMES: Record<string, string> = {
  'www.pbc.gov.cn': '中国人民银行政策资料',
  'data.stats.gov.cn': '国家统计局数据',
  'sousuo.www.gov.cn': '国务院政策文件库',
  'www.gov.cn': '中国政府网',
  'www.csrc.gov.cn': '证监会公开资料',
  'www.safe.gov.cn': '国家外汇管理局资料',
  'www.sse.com.cn': '上海证券交易所资料',
  'www.szse.cn': '深圳证券交易所资料',
  'www.csindex.com.cn': '中证指数资料',
  'oss-ch.csindex.com.cn': '中证指数编制方案',
  'www.cnindex.com.cn': '国证指数资料',
  'www.cninfo.com.cn': '巨潮资讯公告',
  'www.sec.gov': 'SEC / EDGAR 资料',
  'www.federalreserve.gov': '美联储政策资料',
  'www.hkex.com.hk': '香港交易所市场数据',
  'www.nyse.com': '纽约证券交易所日历',
  'www.nasdaq.com': 'Nasdaq 市场资料',
  'developers.binance.com': 'Binance 市场数据文档',
  'investor.nvidia.com': 'NVIDIA 投资者资料',
  'investors.micron.com': 'Micron 投资者资料',
  'news.skhynix.com': 'SK hynix 新闻资料',
  'www.samsung.com': 'Samsung 财务资料',
  'investor.tsmc.com': '台积电投资者资料',
  'computeexpresslink.org': 'CXL 技术规范',
  'www.nist.gov': 'NIST AI 风险框架',
  'docs.langchain.com': 'LangGraph 持久化文档',
}

function formatDate(value?: string | null): string {
  if (!value || value === '未记录') return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function shortHash(value: string): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '—'
}

function safeExternalUrl(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.toString() : undefined
  } catch {
    return undefined
  }
}

function currentD1Copy(value: string): string {
  return value
    .replace(/D1\s*(?:与|和|、|\/)\s*D2/gi, 'D1')
    .replace(/\bD2\b/gi, '历史周期')
}

function sourceMeta(value: string): { host: string; label: string } {
  try {
    const host = new URL(value).hostname.toLowerCase()
    return { host, label: SOURCE_NAMES[host] ?? host.replace(/^www\./, '') }
  } catch {
    return { host: '未识别来源', label: value }
  }
}

function domainIdForEntry(entry: WikiEntry): string {
  const upperId = entry.id.toUpperCase()
  const byId = DOMAIN_DEFINITIONS.find((domain) => domain.idTokens.some((token) => upperId.includes(token)))
  if (byId) return byId.id
  let best = DOMAIN_DEFINITIONS[0]
  let bestScore = -1
  for (const domain of DOMAIN_DEFINITIONS) {
    const score = entry.tags.filter((tag) => domain.tags.includes(tag.toLowerCase())).length
    if (score > bestScore) {
      best = domain
      bestScore = score
    }
  }
  return best.id
}

function buildDomains(data: WorkbenchData): DomainModel[] {
  const assignedSourceIds = new Set(data.entries.flatMap((entry) => entry.sourceIds))
  const assignedSourceUrls = new Set(data.entries.flatMap((entry) => entry.sourceUrls))
  const domains = DOMAIN_DEFINITIONS.map((definition) => {
    const entries = data.entries.filter((entry) => domainIdForEntry(entry) === definition.id)
    const sourceUrls = [...new Set(entries.flatMap((entry) => entry.sourceUrls))]
    const sourceIds = new Set(entries.flatMap((entry) => entry.sourceIds))
    const uploadedSources = data.sources.filter((source) => (
      sourceIds.has(source.id) || Boolean(source.source_url && sourceUrls.includes(source.source_url))
    ))
    return { ...definition, entries, sourceUrls, uploadedSources }
  }).filter((domain) => domain.entries.length || domain.uploadedSources.length)

  const unassigned = data.sources.filter((source) => (
    !assignedSourceIds.has(source.id) && !(source.source_url && assignedSourceUrls.has(source.source_url))
  ))
  if (unassigned.length) {
    domains.push({
      id: 'inbox', label: '材料收件箱',
      description: '已封存、尚未绑定领域或 Wiki 条目的人工上传材料。',
      tags: [], idTokens: [], entries: [], sourceUrls: [], uploadedSources: unassigned,
    })
  }
  return domains
}

function healthForEntry(entry: WikiEntry, health: FeedbackHealth[]): FeedbackHealth | undefined {
  const rank: Record<HealthStatus, number> = { challenged: 0, watching: 1, healthy: 2 }
  return health
    .filter((item) => item.entry_id === entry.id && item.entry_version === entry.version)
    .sort((left, right) => rank[left.status] - rank[right.status])[0]
}

export function App() {
  const [data, setData] = useState<WorkbenchData>()
  const [error, setError] = useState<string>()
  const [refreshing, setRefreshing] = useState(false)
  const [selectedDomainId, setSelectedDomainId] = useState('index')
  const [selectedEntryId, setSelectedEntryId] = useState('')
  const [selectedSection, setSelectedSection] = useState('')
  const [search, setSearch] = useState('')
  const [action, setAction] = useState<string>()
  const [actionError, setActionError] = useState<string>()

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true)
    try {
      const next = await loadWorkbench()
      setData(next)
      setError(undefined)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Wiki API 暂时不可用')
    } finally {
      if (!quiet) setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0)
    const timer = window.setInterval(() => void refresh(true), 30_000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [refresh])

  const domains = useMemo(() => data ? buildDomains(data) : [], [data])
  const selectedDomain = domains.find((domain) => domain.id === selectedDomainId) ?? domains[0]
  const visibleEntries = useMemo(() => {
    if (!selectedDomain) return []
    const needle = search.trim().toLowerCase()
    if (!needle) return selectedDomain.entries
    return selectedDomain.entries.filter((entry) => (
      [entry.id, entry.title, entry.summary, ...entry.tags]
        .some((value) => value.toLowerCase().includes(needle))
    ))
  }, [search, selectedDomain])
  const selectedEntry = selectedDomain?.entries.find((entry) => entry.id === selectedEntryId)
    ?? visibleEntries[0] ?? selectedDomain?.entries[0]
  const resolvedSection = selectedEntry?.sections.some((section) => section.id === selectedSection)
    ? selectedSection
    : selectedEntry?.sections[0]?.id ?? ''
  const selectedHealth = selectedEntry && data ? healthForEntry(selectedEntry, data.health) : undefined
  const selectedEvents = useMemo(() => (data?.events ?? []).filter((event) => (
    event.entry_id === selectedEntry?.id
    && event.entry_version === selectedEntry.version
    && event.section === resolvedSection
  )).sort((left, right) => right.created_at.localeCompare(left.created_at)), [data?.events, resolvedSection, selectedEntry])

  const runAction = async (work: () => Promise<unknown>, success: string) => {
    setAction(undefined)
    setActionError(undefined)
    try {
      await work()
      setAction(success)
      await refresh(true)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '操作失败')
    }
  }

  if (!data && !error) return <LoadingScreen />
  if (!data && error) return <FailureScreen error={error} retry={() => void refresh()} />

  const uniqueSourceUrls = new Set(data?.entries.flatMap((entry) => entry.sourceUrls) ?? [])
  const sectionCount = data?.entries.reduce((count, entry) => count + entry.sections.length, 0) ?? 0
  const published = data?.publications.filter((publication) => publication.status === 'published').length ?? 0

  return (
    <div className="wiki-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true"><BookOpenText size={20} /></div>
          <div><strong>Wiki Atlas</strong><span>forecast-loop · private knowledge workspace</span></div>
        </div>
        <div className="topbar-actions">
          <div className="horizon-identity" aria-label="预测周期 D1 下一交易日">
            <span>预测周期</span><strong>D1</strong><b>下一交易日</b>
          </div>
          <span className={`runtime-state ${data?.mode === 'live' ? 'live' : 'demo'}`}><i />{data?.mode ?? 'unknown'}</span>
          <button className="quiet-button" onClick={() => void refresh()} disabled={refreshing}>
            <RefreshCw size={15} className={refreshing ? 'spinning' : ''} />刷新
          </button>
        </div>
      </header>

      <main>
        <section className="title-row">
          <div>
            <span className="eyebrow">RAW SOURCES → SYNTHESIS → VERSIONED WIKI</span>
            <h1>领域知识库</h1>
            <p>按领域查看每天收集的原始材料、由材料提取出的 Wiki，以及每个版本的来源与审计血缘。人工上传与自动采集进入同一条生长流水线。</p>
          </div>
          <div className="immutable-rule"><ShieldCheck size={18} /><div><span>快照规则</span><strong>历史预测继续引用旧版本</strong></div></div>
        </section>

        {(action || actionError || error) && (
          <div className={`notice ${actionError || error ? 'error' : 'success'}`} role="status">
            {actionError || error ? <AlertCircle size={16} /> : <CheckCircle2 size={16} />}
            <span>{actionError ?? error ?? action}</span>
          </div>
        )}

        <section className="growth-strip" aria-label="Wiki 生长概览">
          <div className="growth-lead"><span>知识状态</span><strong>每天自动收集与生长</strong><small>来源快照不可变 · 综合结论按版本演进</small></div>
          <Metric label="知识领域" value={domains.filter((domain) => domain.id !== 'inbox').length} note="领域目录" tone="green" />
          <Metric label="原始材料" value={uniqueSourceUrls.size + (data?.sources.length ?? 0)} note="外部来源 + 上传" tone="blue" />
          <Metric label="Wiki 条目" value={data?.entries.length ?? 0} note={`${sectionCount} 个稳定 section`} tone="amber" />
          <Metric label="已发布版本" value={published} note="index + log 已同步" tone="green" />
        </section>

        <section className="workspace">
          <DomainRail
            domains={domains}
            selectedId={selectedDomain?.id ?? ''}
            onSelect={(domain) => {
              setSelectedDomainId(domain.id)
              setSelectedEntryId(domain.entries[0]?.id ?? '')
              setSelectedSection(domain.entries[0]?.sections[0]?.id ?? '')
              setSearch('')
            }}
          />

          <section className="knowledge-column">
            {selectedDomain ? (
              <>
                <DomainLineage
                  domain={selectedDomain}
                  visibleEntries={visibleEntries}
                  selectedEntry={selectedEntry}
                  search={search}
                  onSearch={setSearch}
                  onSelectEntry={(entry) => {
                    setSelectedEntryId(entry.id)
                    setSelectedSection(entry.sections[0]?.id ?? '')
                  }}
                />
                {selectedEntry && (
                  <WikiDossier entry={selectedEntry} section={resolvedSection} onSection={setSelectedSection} />
                )}
                {selectedEntry && data && (
                  <WikiFeedbackPanel
                    entry={selectedEntry}
                    section={resolvedSection}
                    health={selectedHealth}
                    events={selectedEvents}
                    sources={data.sources}
                    onSubmit={(input) => runAction(
                      () => createChallenge(input),
                      '问题已作为不可变反馈封存；当前 Wiki 不会因单次反馈被直接改写。',
                    )}
                  />
                )}
              </>
            ) : <EmptyCopy title="尚无知识领域" note="上传第一份材料后即可创建 Wiki 整理任务。" />}
          </section>

          <aside className="operations-rail">
            <SourceUploadCard onRun={(work, success) => runAction(work, success)} />
            {selectedDomain && <CollectionCard domain={selectedDomain} />}
            <PipelineCard data={data!} />
            <MaintenanceCard
              busy={refreshing}
              onSync={() => runAction(async () => { await syncReflectionFeedback() }, '预测反馈已同步到 Wiki 自审队列。')}
              onMaterialize={() => runAction(async () => { await materializeFeedback() }, '已检查达到门槛的修订候选。')}
            />
          </aside>
        </section>
      </main>
    </div>
  )
}

function Metric({ label, value, note, tone }: { label: string; value: number; note: string; tone: string }) {
  return <div className="growth-metric"><i className={tone} /><div><span>{label}</span><strong>{value}</strong><small>{note}</small></div></div>
}

function DomainRail({
  domains, selectedId, onSelect,
}: { domains: DomainModel[]; selectedId: string; onSelect: (domain: DomainModel) => void }) {
  return (
    <aside className="domain-rail panel">
      <div className="rail-head"><div><span className="eyebrow">DOMAIN INDEX</span><h2>领域目录</h2></div><span>{domains.length}</span></div>
      <div className="domain-list">
        {domains.map((domain) => (
          <button key={domain.id} className={selectedId === domain.id ? 'selected' : ''} onClick={() => onSelect(domain)}>
            <FolderOpen size={17} />
            <div><strong>{domain.label}</strong><small>{domain.sourceUrls.length + domain.uploadedSources.length} 份材料 · {domain.entries.length} 篇 Wiki</small></div>
            <ChevronRight size={15} />
          </button>
        ))}
      </div>
      <div className="rail-note"><Archive size={15} /><span>领域是知识货架；每日事实保留在来源快照，不直接写成长期结论。</span></div>
    </aside>
  )
}

function DomainLineage({
  domain, visibleEntries, selectedEntry, search, onSearch, onSelectEntry,
}: {
  domain: DomainModel; visibleEntries: WikiEntry[]; selectedEntry?: WikiEntry
  search: string; onSearch: (value: string) => void; onSelectEntry: (entry: WikiEntry) => void
}) {
  const selectedUrls = new Set(selectedEntry?.sourceUrls ?? [])
  const selectedIds = new Set(selectedEntry?.sourceIds ?? [])
  return (
    <article className="domain-dossier panel">
      <header className="domain-heading">
        <div><span className="eyebrow">KNOWLEDGE DOMAIN</span><h2>{domain.label}</h2><p>{domain.description}</p></div>
        <div className="domain-count"><span>材料 / Wiki</span><strong>{domain.sourceUrls.length + domain.uploadedSources.length} / {domain.entries.length}</strong></div>
      </header>
      <div className="lineage-heading" aria-hidden="true">
        <div><FileText size={16} /><span>原始材料</span></div><ArrowRight size={17} /><div><BookOpenText size={16} /><span>提取出的 Wiki</span></div>
      </div>
      <div className="lineage-grid">
        <section className="source-ledger" aria-label={`${domain.label}原始材料`}>
          {domain.sourceUrls.map((url) => {
            const meta = sourceMeta(url)
            const safeUrl = safeExternalUrl(url)
            if (!safeUrl) {
              return (
                <div key={url} className={selectedUrls.has(url) ? 'source-row linked' : 'source-row'}>
                  <span className="material-kind">外部</span>
                  <div><strong>{meta.label}</strong><small>来源地址未通过安全校验</small></div>
                  <FileText size={14} />
                </div>
              )
            }
            return (
              <a key={url} className={selectedUrls.has(url) ? 'source-row linked' : 'source-row'} href={safeUrl} target="_blank" rel="noopener noreferrer">
                <span className="material-kind">外部</span>
                <div><strong>{meta.label}</strong><small>{meta.host}</small></div>
                {selectedUrls.has(url) ? <Link2 size={14} /> : <ExternalLink size={14} />}
              </a>
            )
          })}
          {domain.uploadedSources.map((source) => (
            <div key={source.id} className={selectedIds.has(source.id) ? 'source-row linked' : 'source-row'}>
              <span className="material-kind upload">上传</span>
              <div><strong>{source.title}</strong><small>{source.filename} · {formatDate(source.captured_at)}</small></div>
              <FileCheck2 size={14} />
            </div>
          ))}
          {!domain.sourceUrls.length && !domain.uploadedSources.length && (
            <EmptyCopy title="尚无已映射材料" note="上传材料或等待下一次自动采集。" />
          )}
        </section>
        <section className="wiki-ledger" aria-label={`${domain.label}提取出的 Wiki`}>
          {domain.entries.length > 3 && (
            <label className="search-field"><Search size={15} /><input value={search} onChange={(event) => onSearch(event.target.value)} placeholder="搜索这个领域的 Wiki" /></label>
          )}
          {visibleEntries.map((entry) => (
            <button key={entry.id} className={selectedEntry?.id === entry.id ? 'wiki-row selected' : 'wiki-row'} onClick={() => onSelectEntry(entry)}>
              <span className="version-seal">v{entry.version}</span>
              <div><strong>{currentD1Copy(entry.title)}</strong><small>{entry.sourceUrls.length + entry.sourceIds.length} 份来源 · {entry.sections.length} 个 section</small></div>
              <ChevronRight size={15} />
            </button>
          ))}
          {!visibleEntries.length && <EmptyCopy title="没有匹配的 Wiki" note="清除搜索条件，或先从材料生成新条目。" />}
        </section>
      </div>
    </article>
  )
}

function WikiDossier({ entry, section, onSection }: { entry: WikiEntry; section: string; onSection: (value: string) => void }) {
  const content = entry.sections.find((item) => item.id === section)?.content || '该 section 暂无正文。'
  return (
    <article className="wiki-dossier panel">
      <header>
        <div><span className="eyebrow">VERSIONED SYNTHESIS</span><h2>{currentD1Copy(entry.title)}</h2><p>{currentD1Copy(entry.summary)}</p></div>
        <div className="version-plate"><span>当前版本</span><strong>v{entry.version}</strong><code>{shortHash(entry.contentHash)}</code></div>
      </header>
      <div className="dossier-meta">
        <span><History size={13} />更新 {formatDate(entry.updatedAt)}</span>
        <span><Link2 size={13} />{entry.sourceUrls.length + entry.sourceIds.length} 份来源</span>
        <span><BookOpenText size={13} />被 {entry.citedByCount} 次预测引用</span>
        <span><ShieldCheck size={13} />{entry.id}</span>
      </div>
      <div className="section-tabs" role="tablist" aria-label="Wiki sections">
        {entry.sections.map((item) => (
          <button role="tab" aria-selected={section === item.id} key={item.id} onClick={() => onSection(item.id)}>{currentD1Copy(item.heading)}</button>
        ))}
      </div>
      <div className="section-copy"><span>稳定 section · {section}</span><p>{currentD1Copy(content)}</p></div>
    </article>
  )
}

function WikiFeedbackPanel({
  entry, section, health, events, sources, onSubmit,
}: {
  entry: WikiEntry; section: string; health?: FeedbackHealth
  events: WorkbenchData['events']; sources: WikiSource[]
  onSubmit: (input: Parameters<typeof createChallenge>[0]) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [issueType, setIssueType] = useState<Exclude<IssueType, 'right_reason'>>('transmission_mapping')
  const [causalStatus, setCausalStatus] = useState<Exclude<CausalStatus, 'unresolved'>>('hypothesis')
  const [confidence, setConfidence] = useState(0.5)
  const [sourceIds, setSourceIds] = useState<string[]>([])
  const [summary, setSummary] = useState('')
  const [pending, setPending] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setPending(true)
    try {
      await onSubmit({ entryId: entry.id, entryVersion: entry.version, section, issueType, causalStatus, confidence, sourceIds, summary })
      setSummary('')
      setSourceIds([])
      setOpen(false)
    } finally { setPending(false) }
  }
  const statusCopy: Record<HealthStatus, string> = { healthy: '稳定', watching: '观察中', challenged: '被挑战' }
  const status = health?.status ?? 'healthy'
  return (
    <article className="feedback-panel panel">
      <button className="feedback-trigger" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <div><ShieldCheck size={18} /><span><strong>报告这个 Wiki 的问题</strong><small>当前 {statusCopy[status]} · {events.length} 条反馈；单次错误不会直接改写 Wiki</small></span></div>
        <span>{open ? '收起' : '查看与记录'}<ChevronRight size={15} /></span>
      </button>
      {open && (
        <div className="feedback-body">
          <div className="feedback-summary">
            <div><span>独立挑战样本</span><strong>{health?.independent_challenge_episodes ?? 0}</strong></div>
            <div><span>挑战权重</span><strong>{(health?.challenge_weight ?? 0).toFixed(2)}</strong></div>
            <div><span>修订候选</span><strong>{health?.review_eligible ? '已达到' : '未达到'}</strong></div>
          </div>
          <form onSubmit={submit}>
            <div className="target-lock"><ShieldCheck size={15} /><span>{entry.id}@{entry.version}</span><code>/{section}</code></div>
            <div className="form-grid">
              <label><span>问题类型</span><select value={issueType} onChange={(event) => setIssueType(event.target.value as typeof issueType)}>{Object.entries(issueLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
              <label><span>因果状态</span><select value={causalStatus} onChange={(event) => setCausalStatus(event.target.value as typeof causalStatus)}><option value="hypothesis">假设</option><option value="supported">有支持</option><option value="verified">已验证</option></select></label>
            </div>
            <label className="confidence-field"><span>归因置信度 <strong>{Math.round(confidence * 100)}%</strong></span><input type="range" min="0" max="1" step="0.05" value={confidence} onChange={(event) => setConfidence(Number(event.target.value))} /></label>
            <label><span>问题说明</span><textarea required minLength={10} maxLength={4000} value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="说明哪里有问题、在什么条件下失效，以及观察到的反例。" /></label>
            {sources.length > 0 && <fieldset><legend>绑定已封存的上传材料</legend><div className="source-checks">{sources.slice(0, 8).map((source) => <label key={source.id}><input type="checkbox" checked={sourceIds.includes(source.id)} onChange={() => setSourceIds((current) => current.includes(source.id) ? current.filter((id) => id !== source.id) : [...current, source.id])} /><span><strong>{source.title}</strong><small>{source.id} · {source.origin}</small></span></label>)}</div></fieldset>}
            <button className="primary-button" disabled={pending || summary.trim().length < 10 || (causalStatus === 'verified' && !sourceIds.length)}>{pending ? <LoaderCircle className="spinning" size={15} /> : <ShieldCheck size={15} />}封存问题反馈</button>
          </form>
          {events.length > 0 && <div className="event-preview">{events.slice(0, 3).map((event) => <div key={event.id}><span>{event.origin === 'human' ? '人工' : '复盘'}</span><p>{event.summary}</p><time>{formatDate(event.created_at)}</time></div>)}</div>}
        </div>
      )}
    </article>
  )
}

function SourceUploadCard({ onRun }: { onRun: (work: () => Promise<unknown>, success: string) => Promise<void> }) {
  const [file, setFile] = useState<File>()
  const [pending, setPending] = useState(false)
  const upload = async () => {
    if (!file) return
    if (file.size > MAX_UPLOAD_BYTES) throw new Error('单个材料不能超过 20 MiB')
    setPending(true)
    try {
      const source = await uploadSource({ title: file.name.replace(/\.[^.]+$/, ''), filename: file.name, media_type: file.type || mediaType(file.name), content_base64: await fileBase64(file) })
      await prepareWikiJob([source.id])
      setFile(undefined)
    } finally { setPending(false) }
  }
  return <article className="operation-card upload-card panel"><div className="operation-title"><UploadCloud size={17} /><div><strong>添加原始材料</strong><span>人工材料与自动采集使用同一流水线</span></div></div><label className="upload-field"><FileUp size={17} /><span>{file?.name ?? '选择 PDF、Markdown、JSON 或图片'}</span><input type="file" accept=".pdf,.md,.txt,.html,.json,.csv,.png,.jpg,.jpeg,.webp" onChange={(event) => setFile(event.target.files?.[0])} /></label><button disabled={!file || pending} onClick={() => void onRun(upload, '材料已按内容哈希封存，并生成 Wiki 整理任务。')}>{pending ? <LoaderCircle className="spinning" size={14} /> : <Database size={14} />}封存并生成整理任务</button></article>
}

function CollectionCard({ domain }: { domain: DomainModel }) {
  const hosts = [...new Set(domain.sourceUrls.map((url) => sourceMeta(url).host))]
  return <article className="operation-card collection-card panel"><div className="operation-title"><Sparkles size={17} /><div><strong>领域采集器</strong><span>自动采集 · 每天更新</span></div></div><p>{domain.label}当前覆盖 {hosts.length} 个外部来源，采集结果先封存为原始快照，再提炼为可版本化 Wiki。</p><div className="collector-hosts">{hosts.slice(0, 6).map((host) => <span key={host}>{host}</span>)}{!hosts.length && <span>等待配置或人工上传</span>}</div></article>
}

function PipelineCard({ data }: { data: WorkbenchData }) {
  const awaiting = data.jobs.filter((job) => job.status === 'awaiting_draft').length
  const ready = data.jobs.filter((job) => job.status === 'draft_ready').length
  const latest = data.publications.filter((item) => item.status === 'published').sort((left, right) => (right.published_at ?? '').localeCompare(left.published_at ?? ''))[0]
  return <article className="operation-card pipeline panel"><div className="operation-title"><FileClock size={17} /><div><strong>Wiki 生长流水线</strong><span>校验通过后自动发布</span></div></div><ol><li><i className={awaiting ? 'active' : ''}>1</i><span><strong>{awaiting} 个任务等待整理</strong><small>模型只生成结构化草稿</small></span></li><li><i className={ready ? 'active' : ''}>2</i><span><strong>{ready} 个草稿等待校验</strong><small>确定性检查版本、来源与哈希</small></span></li><li><i className={latest ? 'done' : ''}>3</i><span><strong>{latest ? `${latest.target_entry_id} → v${latest.proposed_version}` : '尚无新发布'}</strong><small>自动更新 index.md 并追加 log.md</small></span></li></ol></article>
}

function MaintenanceCard({ busy, onSync, onMaterialize }: { busy: boolean; onSync: () => void; onMaterialize: () => void }) {
  return <article className="operation-card panel"><div className="operation-title"><ShieldCheck size={17} /><div><strong>知识自审</strong><span>预测结果反向形成复核信号</span></div></div><p>反馈锁定到具体版本和 section；只有累积到门槛后，才生成下一版本的修订候选。</p><button onClick={onSync} disabled={busy}><RefreshCw size={14} />同步预测反馈</button><button onClick={onMaterialize} disabled={busy}><BookOpenText size={14} />检查修订候选</button></article>
}

function EmptyCopy({ title, note }: { title: string; note: string }) {
  return <div className="empty-copy"><BookOpenText size={20} /><strong>{title}</strong><span>{note}</span></div>
}

function LoadingScreen() {
  return <div className="screen-state"><LoaderCircle className="spinning" size={24} /><strong>正在装载 Wiki 快照</strong><span>读取领域、原始材料与知识版本</span></div>
}

function FailureScreen({ error, retry }: { error: string; retry: () => void }) {
  return <div className="screen-state failure"><AlertCircle size={24} /><strong>无法连接 Wiki API</strong><span>{error}</span><button onClick={retry}>重新连接</button></div>
}

function mediaType(name: string): string {
  const extension = name.split('.').pop()?.toLowerCase()
  return ({ pdf: 'application/pdf', md: 'text/markdown', txt: 'text/plain', html: 'text/html', json: 'application/json', csv: 'text/csv', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' } as Record<string, string>)[extension ?? ''] ?? 'application/octet-stream'
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('无法读取所选文件'))
    reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '')
    reader.readAsDataURL(file)
  })
}
