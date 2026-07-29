from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.config import Settings, get_settings
from fastapi.testclient import TestClient

# Disable the repository dotenv before test modules import ``app.main``. That
# module creates its default application at import time and would otherwise
# cache operator-local settings before an autouse fixture can run.
Settings.model_config["env_file"] = None
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings_from_repository_dotenv() -> Iterator[None]:
    """Keep tests deterministic regardless of the operator's local ``.env``."""

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    from app.main import create_app

    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "methodology.md").write_text(
        """---
id: VC-WIKI-PREDICTION-LABELS
title: 预测标签与证据方法
version: 1.0.0
updated_at: 2026-07-13
published_at: 2026-07-13T00:00:00+08:00
status: active
owners:
  - forecast-loop
tags:
  - prediction
  - macro
  - market
  - risk
  - ai-storage
source_urls:
  - https://example.com/official-source
---
<!-- section:methodology -->
# 方法
所有判断必须冻结时间截面并引用可验证的知识条目。

<!-- section:labels -->
# 标签
D1 与 D2 使用涨跌二元方向、三结果概率和动态评价噪声带。
""",
        encoding="utf-8",
    )
    (wiki_path / "market-strategy.md").write_text(
        """---
id: VC-WIKI-MARKET-STRATEGY
title: 市场策略与指数配置
version: 1.0.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners: [strategy_agent]
tags: [strategy, allocation]
source_urls: [https://www.csindex.com.cn/]
---
<!-- section:synthesis -->
# 策略综合
综合三位研究员并比较五个指数，不重复计算同源证据。
""",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        wiki_path=wiki_path,
        prediction_status_root=tmp_path / "prediction-status",
        user_judgment_wiki_root=tmp_path / "user-wiki",
        demo_mode=True,
        auto_seed=False,
    )
    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as test_client:
        yield test_client
