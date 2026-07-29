"""Synthetic, inert scoring code frozen only as a fixture artifact."""


def score(momentum_5d: float, volatility_20d: float) -> float:
    return 0.8 * momentum_5d - 0.2 * volatility_20d
