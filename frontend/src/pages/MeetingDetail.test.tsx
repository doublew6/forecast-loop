import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'

import { MeetingDetail } from './MeetingDetail'

const { useLatestForecastsMock, useMeetingMock } = vi.hoisted(() => ({
  useLatestForecastsMock: vi.fn(),
  useMeetingMock: vi.fn(),
}))

vi.mock('../lib/api', () => ({
  useLatestForecasts: useLatestForecastsMock,
  useMeeting: useMeetingMock,
}))

function forecast(horizon: 'D1' | 'D2') {
  return {
    id: `FORECAST-${horizon}`,
    run_id: 'RUN-HISTORICAL',
    index_code: 'EXAMPLE.IDX',
    index_name: 'Example Index',
    horizon,
    base_trade_date: '2026-07-10',
    target_date: horizon === 'D1' ? '2026-07-13' : '2026-07-14',
    as_of: '2026-07-10T16:00:00+08:00',
    data_cutoff: '2026-07-10T16:00:00+08:00',
    direction: 'up',
    probabilities: { up: 0.55, neutral: 0.25, down: 0.2 },
    threshold: 0.004,
    confidence: 0.73,
    rationale: `${horizon} historical rationale`,
    citations: [],
    abstain: false,
  }
}

function renderMeeting(horizons: Array<'D1' | 'D2'>) {
  useLatestForecastsMock.mockReturnValue({
    data: undefined,
    isLoading: false,
  })
  useMeetingMock.mockReturnValue({
    data: {
      mode: 'live',
      data: {
        run: {
          id: 'RUN-HISTORICAL',
          as_of: '2026-07-10T16:00:00+08:00',
          data_cutoff: '2026-07-10T16:00:00+08:00',
          status: 'completed',
          mode: 'live',
          data_quality: {},
        },
        opinions: [],
        forecasts: horizons.map(forecast),
        workflow_steps: [],
      },
    },
    isLoading: false,
    isError: false,
    error: null,
  })

  return render(
    <MemoryRouter initialEntries={['/meeting/RUN-HISTORICAL']}>
      <Routes>
        <Route path="/meeting/:runId" element={<MeetingDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MeetingDetail historical horizons', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('defaults to D1 and allows D2 only when the historical run contains it', async () => {
    const user = userEvent.setup()
    renderMeeting(['D1', 'D2'])

    expect(await screen.findByText('Example Index · D1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'D1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    await user.click(screen.getByRole('button', { name: 'D2' }))

    expect(screen.getByText('Example Index · D2')).toBeInTheDocument()
    expect(screen.getByText('D2 historical rationale')).toBeInTheDocument()
  })

  it('does not offer D2 for a current D1-only run', async () => {
    renderMeeting(['D1'])

    expect(await screen.findByText('Example Index · D1')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'D2' })).not.toBeInTheDocument()
  })
})
