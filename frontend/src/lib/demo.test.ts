import { demoForecastBatch, demoOpinions, demoScorecards } from './demo'

describe('fallback demo data', () => {
  it('never invents a Quant opinion and keeps every prediction direction binary', () => {
    expect(demoOpinions.some((opinion) => opinion.agent_id === 'quant_agent')).toBe(false)
    expect(demoOpinions.every((opinion) => ['up', 'down'].includes(opinion.direction))).toBe(true)
    expect(
      demoForecastBatch.forecasts.every((forecast) => ['up', 'down'].includes(forecast.direction)),
    ).toBe(true)
  })

  it('does not invent historical scorecards when the API is unavailable', () => {
    expect(demoScorecards).toHaveLength(14)
    expect(
      demoScorecards.every(
        (scorecard) =>
          scorecard.sample_size === 0
          && scorecard.accuracy === null
          && scorecard.brier === null
          && scorecard.calibration.length === 0,
      ),
    ).toBe(true)
  })
})
