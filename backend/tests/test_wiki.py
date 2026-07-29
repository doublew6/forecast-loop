from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.schemas import Citation
from app.services.wiki import WikiCatalog, parse_frontmatter


def test_frontmatter_and_explicit_sections_are_parsed(tmp_path) -> None:
    text = """---
id: VC-WIKI-TEST
title: Test Entry
version: 1.2.3
updated_at: 2026-07-13
status: active
owners: ["research"]
tags:
  - test
source_urls:
  - https://example.com/source
---
<!-- section:overview -->
# Overview
Grounded content.
"""
    metadata, body = parse_frontmatter(text)
    assert metadata["id"] == "VC-WIKI-TEST"
    assert "Grounded content" in body
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "test.md").write_text(text, encoding="utf-8")
    catalog = WikiCatalog(wiki_path)
    entry = catalog.get("VC-WIKI-TEST")
    assert entry is not None
    assert entry.version == "1.2.3"
    assert entry.sections[0].slug == "overview"
    citation = catalog.citation("macro_policy_agent")
    assert catalog.citation_is_valid(citation)
    tampered = Citation(**{**citation.model_dump(), "content_hash": "0" * 64})
    assert not catalog.citation_is_valid(tampered)


def test_frozen_catalog_is_immune_to_mid_run_file_changes(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    path = wiki_path / "entry.md"
    original = """---
id: VC-WIKI-MACRO-TEST
title: Original
version: 1.0.0
status: active
tags: [macro]
source_urls: ["https://www.pbc.gov.cn/original"]
---
<!-- section:overview -->
# Original section
Original frozen text.
"""
    path.write_text(original, encoding="utf-8")
    catalog = WikiCatalog(wiki_path)
    frozen = catalog.freeze()
    original_citation = frozen.citation("macro_policy_agent")

    path.write_text(original.replace("Original", "Mutated"), encoding="utf-8")

    assert frozen.citation_is_valid(original_citation)
    assert frozen.citation("macro_policy_agent").content_hash == original_citation.content_hash
    assert catalog.citation("macro_policy_agent").content_hash != original_citation.content_hash


def test_wiki_citation_validation_checks_title_quote_and_static_sources(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "entry.md").write_text(
        """---
id: VC-WIKI-MACRO-TEST
title: Macro
version: 1.0.0
status: active
tags: [macro]
source_urls: ["https://www.pbc.gov.cn/source"]
---
<!-- section:overview -->
# Source policy
Frozen methodology.
""",
        encoding="utf-8",
    )
    catalog = WikiCatalog(wiki_path).freeze()
    citation = catalog.citation("macro_policy_agent")

    for update in (
        {"wiki_title": "Forged title"},
        {"wiki_quote": "Forged quote"},
        {"source_urls": ["https://evil.example/forged"]},
    ):
        tampered = citation.model_copy(update=update)
        assert not catalog.citation_is_valid(tampered)


def test_agent_selection_excludes_draft_entries(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "a-draft.md").write_text(
        """---
id: VC-WIKI-MARKET-DRAFT
title: Draft market scaffold
version: 0.1.0
status: draft
tags: [market]
---
<!-- section:overview -->
# Draft
This must not enter a prediction.
""",
        encoding="utf-8",
    )
    (wiki_path / "b-active.md").write_text(
        """---
id: VC-WIKI-MARKET-ACTIVE
title: Active market knowledge
version: 1.0.0
status: active
tags: [market]
---
<!-- section:overview -->
# Active
This entry is approved for predictions.
""",
        encoding="utf-8",
    )

    selected = WikiCatalog(wiki_path).select_for_agent("market_news_agent")

    assert selected.id == "VC-WIKI-MARKET-ACTIVE"


def test_strategy_agent_fails_closed_without_its_pinned_active_entry(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "other.md").write_text(
        """---
id: VC-WIKI-OTHER-STRATEGY
title: Other allocation page
version: 1.0.0
status: active
tags: [strategy, allocation]
---
# Other
This must not silently replace the governed strategy framework.
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="VC-WIKI-MARKET-STRATEGY"):
        WikiCatalog(wiki_path).select_for_agent("strategy_agent")


def test_catalog_excludes_proposals_and_templates(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "published.md").write_text(
        """---
id: VC-WIKI-PUBLISHED
title: Published
version: 1.0.0
status: active
---
# Published
""",
        encoding="utf-8",
    )
    for directory, entry_id in (
        ("proposals", "VC-WIKI-PROPOSAL"),
        ("templates", "VC-WIKI-TEMPLATE"),
    ):
        child = wiki_path / directory
        child.mkdir()
        (child / "ignored.md").write_text(
            f"""---
id: {entry_id}
title: Ignored
version: 1.0.0
status: active
---
# Ignored
""",
            encoding="utf-8",
        )

    catalog = WikiCatalog(wiki_path)

    assert [entry.id for entry in catalog.list_entries()] == ["VC-WIKI-PUBLISHED"]
    assert catalog.get("VC-WIKI-PROPOSAL") is None
    assert catalog.get("VC-WIKI-TEMPLATE") is None


def test_runtime_rejects_catalog_with_only_drafts(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "draft.md").write_text(
        """---
id: VC-WIKI-DRAFT-ONLY
title: Draft only
version: 0.1.0
status: draft
---
# Draft
""",
        encoding="utf-8",
    )
    catalog = WikiCatalog(wiki_path)

    with pytest.raises(RuntimeError, match="no active entries"):
        catalog.select_for_agent("market_news_agent")
    with pytest.raises(RuntimeError, match="no active entries"):
        catalog.freeze()


def test_missing_catalog_uses_demo_fallback_but_live_freeze_fails_closed(tmp_path) -> None:
    catalog = WikiCatalog(tmp_path / "missing-wiki")

    frozen = catalog.freeze()
    assert frozen.select_for_agent("strategy_agent").id == "VC-WIKI-DEMO-METHODOLOGY"
    with pytest.raises(RuntimeError, match="no active entries"):
        catalog.freeze(allow_demo_fallback=False)


def test_runtime_snapshot_excludes_drafts(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    for filename, entry_id, status in (
        ("active.md", "VC-WIKI-ACTIVE", "active"),
        ("draft.md", "VC-WIKI-DRAFT", "draft"),
    ):
        (wiki_path / filename).write_text(
            f"""---
id: {entry_id}
title: Entry
version: 1.0.0
status: {status}
---
# Entry
""",
            encoding="utf-8",
        )

    catalog = WikiCatalog(wiki_path)

    assert {entry.id for entry in catalog.list_entries()} == {
        "VC-WIKI-ACTIVE",
        "VC-WIKI-DRAFT",
    }
    assert [entry.id for entry in catalog.freeze().list_entries()] == ["VC-WIKI-ACTIVE"]


def test_section_markers_inside_code_examples_are_ignored(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "examples.md").write_text(
        """---
id: VC-WIKI-EXAMPLES
title: Examples
version: 1.0.0
status: active
---
<!-- section:anchors -->
## Anchors

    <!-- section:indented-example -->

```markdown
<!-- section:fenced-example -->
```

<!-- section:after -->
## After
Real content.
""",
        encoding="utf-8",
    )

    entry = WikiCatalog(wiki_path).get("VC-WIKI-EXAMPLES")

    assert entry is not None
    assert [section.slug for section in entry.sections] == ["anchors", "after"]


def test_live_freeze_rejects_missing_or_future_publication_time(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    cutoff = datetime(2026, 7, 20, 17, 46, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = wiki_path / "entry.md"
    base = """---
id: VC-WIKI-LIVE-CUTOFF
title: Live cutoff
version: 1.0.0
status: active
tags: [macro]
{published_at}---
# Live cutoff
"""
    path.write_text(base.format(published_at=""), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unpublished or newer"):
        WikiCatalog(wiki_path).freeze(
            allow_demo_fallback=False,
            cutoff=cutoff,
        )

    path.write_text(
        base.format(published_at="published_at: 2026-07-20T17:47:00+08:00\n"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="VC-WIKI-LIVE-CUTOFF"):
        WikiCatalog(wiki_path).freeze(
            allow_demo_fallback=False,
            cutoff=cutoff,
        )

    path.write_text(
        base.format(published_at="published_at: 2026-07-20T17:46:00+08:00\n"),
        encoding="utf-8",
    )
    frozen = WikiCatalog(wiki_path).freeze(
        allow_demo_fallback=False,
        cutoff=cutoff,
    )

    assert frozen.list_entries()[0].published_at == cutoff
    assert frozen.snapshot()[0]["published_at"] == cutoff.isoformat()


def test_live_freeze_excludes_entries_published_after_the_historical_cutoff(
    tmp_path,
) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    cutoff = datetime(2026, 7, 27, 19, 44, tzinfo=ZoneInfo("Asia/Shanghai"))
    (wiki_path / "eligible.md").write_text(
        """---
id: VC-WIKI-ELIGIBLE
title: Eligible
version: 1.0.0
published_at: 2026-07-27T00:00:00+08:00
status: active
tags: [macro]
---
# Eligible
""",
        encoding="utf-8",
    )
    (wiki_path / "future.md").write_text(
        """---
id: VC-WIKI-FUTURE
title: Future
version: 1.0.0
published_at: 2026-07-29T00:00:00+08:00
status: active
tags: [global-equity]
---
# Future
""",
        encoding="utf-8",
    )

    frozen = WikiCatalog(wiki_path).freeze(
        allow_demo_fallback=False,
        cutoff=cutoff,
    )

    assert [entry.id for entry in frozen.list_entries()] == ["VC-WIKI-ELIGIBLE"]


def test_demo_freeze_remains_isolated_from_live_publication_cutoff(tmp_path) -> None:
    catalog = WikiCatalog(tmp_path / "missing-wiki")

    demo = catalog.freeze()

    assert demo.list_entries()[0].status == "demo-only"
    assert demo.list_entries()[0].published_at is None
    with pytest.raises(RuntimeError, match="no active entries"):
        catalog.freeze(
            allow_demo_fallback=False,
            cutoff=datetime(
                2026,
                7,
                20,
                17,
                46,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )
