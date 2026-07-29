import { BookMarked, Clock3, ExternalLink, FileCheck2, Link2, Search, ShieldCheck } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'

import { DemoBanner, EmptyState, LoadingPanel, PageHeading } from '../components/Common'
import { useWikiEntries } from '../lib/api'
import { formatDateTime } from '../lib/format'

export function Wiki() {
  const query = useWikiEntries()
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const entries = useMemo(() => query.data?.data ?? [], [query.data?.data])
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return entries
    return entries.filter((item) =>
      [item.id, item.title, item.category, item.summary].some((field) => field.toLowerCase().includes(needle)),
    )
  }, [entries, search])
  const requestedEntryId = searchParams.get('entry')
  const requestedSection = searchParams.get('section')
  const selected = entries.find((item) => item.id === requestedEntryId) ?? filtered[0]
  const targetSection = useMemo(() => {
    if (!selected || !requestedSection) return undefined
    const normalize = (value: string) => value.toLowerCase().replace(/^§\s*\d+\s*/, '').replace(/[\s_-]+/g, '')
    return selected.sections.find((section) =>
      section.id === requestedSection
      || section.heading === requestedSection
      || normalize(section.heading) === normalize(requestedSection),
    )
  }, [requestedSection, selected])

  useEffect(() => {
    if (!selected || !targetSection) return
    const element = document.getElementById(`${selected.id}-${targetSection.id}`)
    element?.scrollIntoView?.({ block: 'start' })
  }, [selected, targetSection])

  const selectEntry = (entryId: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('entry', entryId)
    next.delete('section')
    setSearchParams(next)
  }

  if (query.isLoading) return <LoadingPanel />

  return (
    <div className="page wiki-page">
      <PageHeading
        eyebrow="证据知识库"
        title="可验证知识库"
        description="每个决策引用稳定的 Wiki 段落；每个 Wiki 条目再追溯到原始资料。"
        actions={<div className="wiki-stat"><BookMarked size={18} /><div><span>知识条目</span><strong>{entries.length}</strong></div></div>}
      />
      {query.data?.mode === 'demo' && <DemoBanner error={query.data.error} reason={query.data.demo_reason} />}

      <div className="wiki-toolbar">
        <div className="search-box"><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索条目、ID 或分类" aria-label="搜索 Wiki" /></div>
        <div className="wiki-principle"><ShieldCheck size={16} /><span>模型只能提议修改，不能在同次决策中自写自引</span></div>
      </div>

      <div className="wiki-layout">
        <aside className="wiki-index panel">
          <div className="wiki-index-head"><span>{filtered.length} 个结果</span><small>按最近更新</small></div>
          <div className="wiki-entry-list">
            {filtered.map((entry) => (
              <button key={entry.id} className={selected?.id === entry.id ? 'active' : ''} onClick={() => selectEntry(entry.id)}>
                <div><span className="wiki-category">{entry.category}</span><span className="wiki-version">v{entry.version}</span></div>
                <strong>{entry.title}</strong>
                <small>{entry.id}</small>
                <p>{entry.summary}</p>
                <footer><span><Clock3 size={12} /> {formatDateTime(entry.updated_at)}</span><span><Link2 size={12} /> {entry.cited_by_count} 次引用</span></footer>
              </button>
            ))}
          </div>
        </aside>

        <section className="wiki-document panel">
          {selected ? (
            <>
              <header className="wiki-document-head">
                <div><span>{selected.category} / {selected.id}</span><h2>{selected.title}</h2><p>{selected.summary}</p></div>
                <div className="document-version"><span>当前版本</span><strong>v{selected.version}</strong></div>
              </header>
              <div className="wiki-callout"><FileCheck2 size={17} /><div><strong>历史决策引用不可变</strong><span>条目更新不会改变旧决策中的 Wiki 快照与内容哈希。</span></div></div>
              <div className="wiki-sections">
                {selected.sections.map((section) => (
                  <article
                    key={section.id}
                    id={`${selected.id}-${section.id}`}
                    className={targetSection?.id === section.id ? 'citation-target' : undefined}
                  >
                    <h3>{section.heading}<a href={`#${selected.id}-${section.id}`} aria-label="复制段落定位"><Link2 size={14} /></a></h3>
                    <p>{section.content}</p>
                  </article>
                ))}
              </div>
              <div className="source-section">
                <div className="source-heading"><span className="eyebrow">来源追溯</span><h3>原始资料</h3></div>
                {selected.sources.length ? (
                  <div className="source-list">
                    {selected.sources.map((source) => (
                      <a href={source.url} target="_blank" rel="noreferrer" key={source.id}>
                        <div className="source-icon"><FileCheck2 size={17} /></div>
                        <div><span>{source.publisher} · {source.published_at}</span><strong>{source.title}</strong><small>{source.content_hash}</small></div>
                        <ExternalLink size={15} />
                      </a>
                    ))}
                  </div>
                ) : <EmptyState title="无外部来源" description="该条目定义系统内部评价口径。" />}
              </div>
            </>
          ) : <EmptyState title="没有匹配条目" description="尝试清空搜索关键词。" />}
        </section>
      </div>
    </div>
  )
}
