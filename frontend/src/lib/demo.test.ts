import {
  demoForecastBatch,
  demoMeeting,
  demoOpinions,
  demoRuns,
  demoScorecards,
} from './demo'

describe('fallback demo data', () => {
  it('never invents a Quant opinion and keeps every prediction direction binary', () => {
    expect(demoOpinions.some((opinion) => opinion.agent_id === 'quant_agent')).toBe(false)
    expect(demoOpinions.every((opinion) => ['up', 'down'].includes(opinion.direction))).toBe(true)
    expect(
      demoForecastBatch.forecasts.every((forecast) => ['up', 'down'].includes(forecast.direction)),
    ).toBe(true)
  })

  it('keeps the current D1 demo run and its forecast counts consistent', () => {
    const currentRun = demoRuns.find((run) => run.id === demoForecastBatch.run_id)

    expect(demoForecastBatch.forecasts).toHaveLength(5)
    expect(demoForecastBatch.forecasts.every((forecast) => forecast.horizon === 'D1')).toBe(true)
    expect(demoOpinions.every((opinion) => opinion.horizon === 'D1')).toBe(true)
    expect(demoMeeting.run.forecasts_count).toBe(demoForecastBatch.forecasts.length)
    expect(currentRun?.forecasts_count).toBe(demoForecastBatch.forecasts.length)
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
