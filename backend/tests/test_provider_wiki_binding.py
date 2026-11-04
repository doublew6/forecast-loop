from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.domain import Horizon, IndexDefinition
from app.schemas import AgentDraft
from app.services.provider import LangChainResearchProvider
from app.services.wiki import WikiCatalog


class SchemaAwareModel:
    def __init__(self, **_kwargs):
        self.schemas = []
        self.invalid = False

    def with_structured_output(self, schema, **_kwargs):
        self.schemas.append(schema)
        owner = self

        class Bound:
            def invoke(self, _prompt):
                constrained = (
                    isinstance(schema, dict) and "enum" in schema["properties"]["wiki_section"]
                )
                entry = (
                    schema["properties"]["wiki_entry_id"]["enum"][0] if constrained else "entry-a"
                )
                slug = (
                    schema["properties"]["wiki_section"]["enum"][0]
                    if constrained
                    else "section:methodology"
                )
                if owner.invalid:
                    slug = "unavailable"
                evidence_ids = (
                    []
                    if constrained
                    and schema["properties"]["evidence_item_ids"].get("maxItems") == 0
                    else ["source-a"]
                )
                return AgentDraft(
                    direction="up",
                    probabilities={"up": 0.4, "neutral": 0.3, "down": 0.3},
                    summary="Synthetic analysis",
                    evidence=["source-a"],
                    evidence_item_ids=evidence_ids,
                    wiki_entry_id=entry,
                    wiki_section=slug,
                )

        return Bound()


def setup_provider(tmp_path, monkeypatch):
    for identity, tags in [
        ("entry-a", "macro, market, risk, ai-storage"),
        ("VC-WIKI-MARKET-STRATEGY", "strategy, allocation"),
    ]:
        (tmp_path / (identity + ".md")).write_text(
            f"---\nid: {identity}\ntitle: Synthetic entry\nversion: 1.0.0\n"
            f"status: active\ntags: [{tags}]\n---\n"
            "<!-- section:methodology -->\n# Methodology\nSynthetic policy.\n"
        )
    raw = SchemaAwareModel()
    monkeypatch.setattr("app.services.provider.ChatOpenAI", lambda **kwargs: raw)
    provider = LangChainResearchProvider(Settings(_env_file=None))
    return provider, raw, WikiCatalog(tmp_path)


def invoke(provider, wiki, role):
    now = datetime(2026, 1, 5, 16, tzinfo=UTC)
    common = dict(
        index=IndexDefinition("SYNTH.US", "Synthetic", market="US"),
        horizon=Horizon.D1,
        as_of=now,
        wiki=wiki,
    )
    if role == "research":
        return provider.research(
            **common,
            agent_id="macro_policy_agent",
            evidence_snapshot=SimpleNamespace(
                items=[], volatility_20d={"SYNTH.US": 0.01}, data_cutoff=now
            ),
        )
    opinions = [{"evidence_item_ids": ["source-a"]}]
    if role == "critic":
        return provider.criticize(**common, research_opinions=opinions)
    return provider.strategize(
        **common, data_cutoff=now, volatility_20d=0.01, research_opinions=opinions, peer_opinions=[]
    )


@pytest.mark.parametrize("role", ["research", "critic", "strategy"])
def test_all_roles_bind_exact_wiki_ids_and_section_slugs(tmp_path, monkeypatch, role):
    provider, raw, wiki = setup_provider(tmp_path, monkeypatch)
    result = invoke(provider, wiki, role)
    assert result.wiki_section == "methodology"
    schema = raw.schemas[-1]
    assert schema["properties"]["wiki_section"]["enum"] == ["methodology"]
    assert schema["properties"]["wiki_entry_id"]["enum"] == [result.wiki_entry_id]
    assert "wiki_section" in schema["required"]


@pytest.mark.parametrize("role", ["research", "critic", "strategy"])
def test_post_validation_rejects_provider_disobeying_section_schema(tmp_path, monkeypatch, role):
    provider, raw, wiki = setup_provider(tmp_path, monkeypatch)
    raw.invalid = True
    with pytest.raises(ValueError, match="unavailable Wiki section"):
        invoke(provider, wiki, role)


def test_critic_schema_cannot_introduce_new_evidence(tmp_path, monkeypatch):
    provider, raw, wiki = setup_provider(tmp_path, monkeypatch)
    result = invoke(provider, wiki, "critic")
    assert result.evidence_item_ids == []
    assert raw.schemas[-1]["properties"]["evidence_item_ids"]["maxItems"] == 0
