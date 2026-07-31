import type { Horizon } from '../lib/types'

export function ForecastTargetSelector({
  instruments,
  indexCode,
  horizon,
  onIndexChange,
  onHorizonChange,
  horizons = ['D1', 'D2'],
  availableIndexCodes,
  availableTargets,
  disabled = false,
}: {
  instruments: Array<{ code: string; name: string }>
  indexCode: string
  horizon: Horizon
  onIndexChange: (indexCode: string) => void
  onHorizonChange: (horizon: Horizon) => void
  horizons?: Horizon[]
  availableIndexCodes?: Set<string>
  availableTargets?: Set<string>
  disabled?: boolean
}) {
  return (
    <div className="meeting-selector">
      <div className="index-tabs" aria-label="选择预测标的">
        {instruments.map((instrument) => {
          const available = availableIndexCodes?.has(instrument.code) ?? true
          return (
            <button
              key={instrument.code}
              type="button"
              disabled={disabled || !available}
              aria-pressed={indexCode === instrument.code}
              onClick={() => onIndexChange(instrument.code)}
              className={indexCode === instrument.code ? 'active' : ''}
            >
              <strong>{instrument.name}</strong><span>{instrument.code}</span>
            </button>
          )
        })}
      </div>
      <div className="horizon-pills" aria-label="选择预测周期">
        {horizons.map((item) => (
          <button
            key={item}
            type="button"
            disabled={
              disabled
              || (availableTargets
                ? !availableTargets.has(`${indexCode}:${item}`)
                : false)
            }
            aria-pressed={horizon === item}
            className={horizon === item ? 'active' : ''}
            onClick={() => onHorizonChange(item)}
          >
            {item}
          </button>
        ))}
      </div>
    </div>
  )
}
