import { BookOpenText, ExternalLink, FileCheck2 } from 'lucide-react'
import { Link } from 'react-router'

import type { Citation } from '../lib/types'

export function CitationList({ citations, dense = false }: { citations: Citation[]; dense?: boolean }) {
  if (!citations.length) return <div className="citation-empty">此意见没有引用（占位输出）</div>

  return (
    <div className={`citation-list${dense ? ' dense' : ''}`}>
      {citations.map((citation) => {
        const content = (
          <>
            <div className={`citation-icon ${citation.kind}`}>
              {citation.kind === 'wiki' ? <BookOpenText size={16} /> : <FileCheck2 size={16} />}
            </div>
            <div className="citation-content">
              <div className="citation-kicker">
                {citation.kind === 'wiki' ? '内部 Wiki' : citation.publisher || '原始来源'}
                {citation.version && <span>v{citation.version}</span>}
              </div>
              <strong>{citation.title}</strong>
              {(citation.section || citation.published_at) && (
                <small>{citation.section || citation.published_at?.slice(0, 10)}</small>
              )}
              {!dense && citation.excerpt && <p>“{citation.excerpt}”</p>}
            </div>
            {citation.source_url && <ExternalLink className="citation-link-icon" size={14} />}
          </>
        )

        if (citation.source_url) {
          return (
            <a key={citation.id} className="citation-item" href={citation.source_url} target="_blank" rel="noreferrer">
              {content}
            </a>
          )
        }
        if (citation.kind === 'wiki' && citation.wiki_entry_id) {
          const search = new URLSearchParams({ entry: citation.wiki_entry_id })
          if (citation.section) search.set('section', citation.section)
          return (
            <Link key={citation.id} className="citation-item" to={{ pathname: '/wiki', search: search.toString() }}>
              {content}
            </Link>
          )
        }
        return <div key={citation.id} className="citation-item">{content}</div>
      })}
    </div>
  )
}
