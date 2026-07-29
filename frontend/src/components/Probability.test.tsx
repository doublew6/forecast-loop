import { render, screen } from '@testing-library/react'

import { DemoBanner } from './Common'
import { ProbabilityBar } from './Probability'

describe('ProbabilityBar', () => {
  it('renders all three directions with formatted probabilities', () => {
    render(<ProbabilityBar probabilities={{ up: 0.52, neutral: 0.31, down: 0.17 }} />)

    expect(screen.getByText('上涨')).toBeInTheDocument()
    expect(screen.getByText('52%')).toBeInTheDocument()
    expect(screen.getByText('小波动')).toBeInTheDocument()
    expect(screen.getByText('31%')).toBeInTheDocument()
    expect(screen.getByText('下跌')).toBeInTheDocument()
    expect(screen.getByText('17%')).toBeInTheDocument()
  })
})

describe('DemoBanner', () => {
  it('distinguishes a connected demo provider from an API fallback', () => {
    const { rerender } = render(
      <DemoBanner reason="server" dataAsOf="2026-07-13T15:30:00+08:00" />,
    )
    expect(screen.getByText(/后端已连接/)).toBeInTheDocument()
    expect(screen.getByText(/演示数据截面：2026-07-13/)).toBeInTheDocument()

    rerender(<DemoBanner reason="fallback" error="network error" />)
    expect(screen.getByText(/后端接口不可用/)).toHaveTextContent('network error')
  })
})
