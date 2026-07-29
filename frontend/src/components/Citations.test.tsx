import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'

import type { Citation } from '../lib/types'
import { CitationList } from './Citations'

const wikiCitation: Citation = {
  id: 'wiki-citation',
  title: 'AI 存储产业链',
  kind: 'wiki',
  wiki_entry_id: 'VC-WIKI-AI-STORAGE',
  section: 'hbm-demand',
  version: '1.2.0',
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}{location.search}</div>
}

describe('CitationList', () => {
  it('navigates Wiki citations to their exact entry and section', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<CitationList citations={[wikiCitation]} />} />
          <Route path="/wiki" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(screen.getByRole('link', { name: /AI 存储产业链/ }))

    expect(screen.getByTestId('location')).toHaveTextContent(
      '/wiki?entry=VC-WIKI-AI-STORAGE&section=hbm-demand',
    )
  })
})
