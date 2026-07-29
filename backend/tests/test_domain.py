from __future__ import annotations

import math

import pytest
from app.domain import (
    AgentDefinition,
    Direction,
    Horizon,
    classify_return,
    directional_confidence,
    multiclass_brier_score,
    neutral_threshold,
    predicted_direction,
    realized_return,
    sample_volatility,
    validate_probabilities,
)


def test_agent_definition_requires_explicit_role_and_source() -> None:
    with pytest.raises(TypeError):
        AgentDefinition(
            id="example_agent",
            name="Example Agent",
            role="Produces an example signal.",
            kind="research",
        )


def test_dynamic_neutral_threshold_scales_d2_by_sqrt_two() -> None:
    d1 = neutral_threshold(0.012, Horizon.D1)
    d2 = neutral_threshold(0.012, Horizon.D2)
    assert d1 == pytest.approx(0.003)
    assert d2 == pytest.approx(d1 * math.sqrt(2))


@pytest.mark.parametrize(
    ("actual_return", "expected"),
    [
        (0.0101, Direction.UP),
        (0.01, Direction.NEUTRAL),
        (0.0, Direction.NEUTRAL),
        (-0.01, Direction.NEUTRAL),
        (-0.0101, Direction.DOWN),
    ],
)
def test_return_classification_uses_open_outer_bounds(
    actual_return: float, expected: Direction
) -> None:
    assert classify_return(actual_return, 0.01) is expected


def test_probability_validation_direction_and_brier() -> None:
    probabilities = {"up": 0.7, "neutral": 0.2, "down": 0.1}
    assert validate_probabilities(probabilities) == probabilities
    assert predicted_direction(probabilities) is Direction.UP
    assert multiclass_brier_score(probabilities, Direction.UP) == pytest.approx(
        (0.3**2 + 0.2**2 + 0.1**2) / 3
    )


def test_direction_is_binary_even_when_neutral_probability_is_largest() -> None:
    probabilities = {"up": 0.21, "neutral": 0.60, "down": 0.19}
    assert predicted_direction(probabilities) is Direction.UP
    assert directional_confidence(probabilities) == pytest.approx(0.525)


def test_tied_directional_probabilities_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not be tied"):
        predicted_direction({"up": 0.4, "neutral": 0.2, "down": 0.4})


def test_invalid_probabilities_are_rejected() -> None:
    with pytest.raises(ValueError, match="sum to one"):
        validate_probabilities({"up": 0.5, "neutral": 0.5, "down": 0.5})


def test_return_and_sample_volatility() -> None:
    assert realized_return(100, 102) == pytest.approx(0.02)
    assert sample_volatility([0.01, -0.01]) == pytest.approx(math.sqrt(0.0002))
    with pytest.raises(ValueError):
        realized_return(0, 1)
    with pytest.raises(ValueError):
        sample_volatility([0.1])
