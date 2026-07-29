import type { Direction, Probabilities } from '../lib/types'
import { directionLabel, percent } from '../lib/format'

const keys: Direction[] = ['up', 'neutral', 'down']
const probabilityLabel: Record<Direction, string> = {
  up: directionLabel.up,
  neutral: '小波动',
  down: directionLabel.down,
}

export function ProbabilityBar({ probabilities, compact = false }: { probabilities: Probabilities; compact?: boolean }) {
  return (
    <div className={`probability-block${compact ? ' compact' : ''}`}>
      <div className="probability-track" aria-label="上涨、小波动、下跌结果概率">
        {keys.map((key) => (
          <span
            key={key}
            className={`probability-segment ${key}`}
            style={{ width: `${probabilities[key] * 100}%` }}
          />
        ))}
      </div>
      <div className="probability-labels">
        {keys.map((key) => (
          <span key={key} className={key}>
            <i /> {probabilityLabel[key]} <strong>{percent(probabilities[key])}</strong>
          </span>
        ))}
      </div>
    </div>
  )
}
