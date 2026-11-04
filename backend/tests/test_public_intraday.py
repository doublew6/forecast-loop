"""Synthetic supplements stay explicit and outside scored forecast collections."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.public_intraday import (
    PublicIntradayForecast,
    PublicIntradaySnapshot,
    include_intraday,
    parse_publication,
)
from app.public_showcase import PublicSnapshot


def row():
    return dict(
        id="c" * 64,
        request_hash="d" * 64,
        target_id="csi1000-open-to-open-d1",
        publication_kind="intraday_supplement",
        performance_included=False,
        evidence_status="partial",
        anchor_date="2026-08-13",
        target_date="2026-08-14",
        data_cutoff="2026-08-13T10:13:16+08:00",
        published_at="2026-08-13T10:14:48+08:00",
        probabilities=dict(up=0.35, neutral=0.27, down=0.38),
        threshold=0.005,
    )


def test_supplement_preserves_original_forecasts_and_actual_time():
    base = PublicSnapshot.model_validate(
        dict(
            schema_version="forecast-loop.public-showcase/v1",
            generated_at="2026-08-13T10:00:00+08:00",
            next_update_at="2026-08-14T10:00:00+08:00",
            mode="live",
            forecasts=[
                dict(
                    id="a" * 64,
                    target_id="csi1000-absolute-d1",
                    lane="shadow",
                    anchor_date="2026-08-12",
                    target_date="2026-08-13",
                    data_cutoff="2026-08-12T18:00:00+08:00",
                    published_at="2026-08-12T20:30:00+08:00",
                    probabilities=dict(up=0.6, neutral=0.25, down=0.15),
                    threshold=0.003,
                    result=None,
                )
            ],
        )
    )
    supplement = PublicIntradaySnapshot(
        schema_version="forecast-loop.public-intraday/v1",
        generated_at=datetime(2026, 8, 13, 11, tzinfo=ZoneInfo("Asia/Shanghai")),
        forecasts=[PublicIntradayForecast(**row())],
    )
    combined = include_intraday(base, supplement)
    assert combined.forecasts == base.forecasts
    assert combined.intraday_forecasts[0].published_at.hour == 10
    assert parse_publication(combined.model_dump_json()) == combined
    assert parse_publication(base.model_dump_json()) == base
    with pytest.raises(ValueError):
        PublicSnapshot.model_validate_json(combined.model_dump_json())


@pytest.mark.parametrize(
    "field,value",
    [
        ("performance_included", True),
        ("publication_kind", "premarket"),
        ("published_at", "2026-08-13T09:15:00+08:00"),
        ("data_cutoff", "2026-08-13T11:00:00+08:00"),
        ("rationale", "research text"),
        ("probabilities", dict(up=0.8, neutral=0.3, down=0.2)),
    ],
)
def test_supplement_rejects_backdating_scoring_and_private_prose(field, value):
    value_row = row()
    value_row[field] = value
    with pytest.raises(ValueError):
        PublicIntradayForecast(**value_row)
