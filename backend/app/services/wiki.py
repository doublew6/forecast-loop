"""Versioned Markdown Wiki discovery and citation snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..schemas import Citation, WikiEntryRead, WikiSection

if TYPE_CHECKING:
    from ..config import Settings

SECTION_RE = re.compile(
    r"^<!--\s*section:([a-zA-Z0-9_-]+)\s*-->\s*$",
    re.MULTILINE,
)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
FENCED_CODE_RE = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
UNPUBLISHED_DIR_NAMES = frozenset({"proposals", "templates"})
PINNED_AGENT_ENTRY_IDS = {
    "strategy_agent": "VC-WIKI-MARKET-STRATEGY",
}


@dataclass(frozen=True, slots=True)
class WikiDocument:
    entry: WikiEntryRead
    path: Path | None


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value.replace("'", '"'))
        except json.JSONDecodeError:
            return [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the deliberately small YAML subset used by the repository Wiki."""

    if not text.startswith("---"):
        return {}, text
    closing = text.find("\n---", 3)
    if closing < 0:
        return {}, text
    raw = text[3:closing].strip("\n")
    body = text[closing + 4 :].lstrip("\n")
    metadata: dict[str, Any] = {}
    active_list: str | None = None
    for line in raw.splitlines():
        if line.startswith(("  - ", "- ")) and active_list:
            metadata.setdefault(active_list, []).append(_scalar(line.split("-", 1)[1]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        parsed = _scalar(value)
        if parsed == "":
            metadata[key] = []
            active_list = key
        else:
            metadata[key] = parsed
            active_list = None
    return metadata, body


def _sections(body: str) -> list[WikiSection]:
    searchable_body = FENCED_CODE_RE.sub(_mask_non_newline_characters, body)
    matches = list(SECTION_RE.finditer(searchable_body))
    if not matches:
        title_match = HEADING_RE.search(body)
        return [
            WikiSection(
                slug="overview",
                title=title_match.group(1) if title_match else "概览",
                excerpt=" ".join(body.split())[:240],
            )
        ]
    sections: list[WikiSection] = []
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        title_match = HEADING_RE.search(content)
        sections.append(
            WikiSection(
                slug=match.group(1),
                title=title_match.group(1) if title_match else match.group(1),
                excerpt=" ".join(content.split())[:240],
            )
        )
    return sections


def _mask_non_newline_characters(match: re.Match[str]) -> str:
    """Preserve offsets while hiding fenced examples from section discovery."""

    return "".join("\n" if character == "\n" else " " for character in match.group(0))


class WikiCatalog:
    def __init__(self, path: Path, *, demo_path: Path | None = None) -> None:
        self.path = path
        self.demo_path = demo_path

    @classmethod
    def from_settings(cls, settings: Settings) -> WikiCatalog:
        """Load local operator Wiki first and bundled examples only in Demo."""

        return cls(
            settings.wiki_path,
            demo_path=(
                settings.bundled_wiki_path
                if settings.use_demo_provider
                else None
            ),
        )

    def documents(self, *, include_body: bool = False) -> list[WikiDocument]:
        documents = self._documents_from_path(
            self.path,
            include_body=include_body,
        )
        if (
            not documents
            and self.demo_path is not None
            and self.demo_path.resolve() != self.path.resolve()
        ):
            documents = self._documents_from_path(
                self.demo_path,
                include_body=include_body,
            )
        if not documents:
            documents.append(self._fallback_document(include_body=include_body))
        return documents

    @staticmethod
    def _documents_from_path(
        root: Path,
        *,
        include_body: bool,
    ) -> list[WikiDocument]:
        documents: list[WikiDocument] = []
        if root.exists():
            for path in sorted(root.rglob("*.md")):
                relative_directories = path.relative_to(root).parts[:-1]
                if UNPUBLISHED_DIR_NAMES.intersection(relative_directories):
                    continue
                text = path.read_text(encoding="utf-8")
                metadata, body = parse_frontmatter(text)
                if not metadata.get("id"):
                    continue
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                updated_at = None
                if metadata.get("updated_at"):
                    try:
                        updated_at = date.fromisoformat(str(metadata["updated_at"]))
                    except ValueError:
                        pass
                published_at = None
                if metadata.get("published_at"):
                    try:
                        candidate = datetime.fromisoformat(
                            str(metadata["published_at"])
                        )
                        if (
                            candidate.tzinfo is not None
                            and candidate.utcoffset() is not None
                        ):
                            published_at = candidate
                    except ValueError:
                        pass
                entry = WikiEntryRead(
                    id=str(metadata["id"]),
                    title=str(metadata.get("title", metadata["id"])),
                    version=str(metadata.get("version", "0.0.0")),
                    updated_at=updated_at,
                    published_at=published_at,
                    status=str(metadata.get("status", "unknown")),
                    owners=_as_list(metadata.get("owners", [])),
                    tags=_as_list(metadata.get("tags", [])),
                    source_urls=_as_list(metadata.get("source_urls", [])),
                    sections=_sections(body),
                    content_hash=content_hash,
                    body=body if include_body else None,
                )
                documents.append(WikiDocument(entry=entry, path=path))
        return documents

    def list_entries(self) -> list[WikiEntryRead]:
        return [document.entry for document in self.documents()]

    def freeze(
        self,
        *,
        allow_demo_fallback: bool = True,
        cutoff: datetime | None = None,
    ) -> FrozenWikiCatalog:
        """Read the complete catalog once and detach it from the filesystem."""

        documents = self._runtime_documents(
            self.documents(include_body=True),
            allow_demo_fallback=allow_demo_fallback,
        )
        if cutoff is not None:
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                raise RuntimeError("Wiki cutoff must be timezone-aware")
            visible = [
                document
                for document in documents
                if document.entry.published_at is not None
                and document.entry.published_at <= cutoff
            ]
            if not visible:
                invalid = [
                    document.entry.id
                    for document in documents
                    if document.entry.published_at is None
                    or document.entry.published_at > cutoff
                ]
                raise RuntimeError(
                    "Live Wiki entries are unpublished or newer than the evidence cutoff: "
                    + ", ".join(sorted(invalid))
                )
            documents = visible
        return FrozenWikiCatalog([document.entry for document in documents])

    def get(self, entry_id: str, *, include_body: bool = True) -> WikiEntryRead | None:
        return next(
            (
                document.entry
                for document in self.documents(include_body=include_body)
                if document.entry.id == entry_id
            ),
            None,
        )

    def select_for_agent(
        self,
        agent_id: str,
        *,
        index_code: str | None = None,
        preferred_entry_id: str | None = None,
    ) -> WikiEntryRead:
        selectable_documents = self._runtime_documents(self.documents())
        if preferred_entry_id is not None:
            for document in selectable_documents:
                if document.entry.id == preferred_entry_id:
                    return document.entry
            raise RuntimeError(
                f"configured active Wiki entry {preferred_entry_id} is unavailable "
                f"for {agent_id}"
            )
        pinned_entry_id = PINNED_AGENT_ENTRY_IDS.get(agent_id)
        if pinned_entry_id:
            for document in selectable_documents:
                if document.entry.id == pinned_entry_id:
                    return document.entry
            if len(selectable_documents) == 1 and (
                selectable_documents[0].entry.id == "VC-WIKI-DEMO-METHODOLOGY"
                and selectable_documents[0].entry.status.lower() == "demo-only"
            ):
                return selectable_documents[0].entry
            raise RuntimeError(
                f"required active Wiki entry {pinned_entry_id} is unavailable for {agent_id}"
            )
        index_tokens = {
            "000300.SH": "INDEX-CSI300",
            "000905.SH": "INDEX-CSI500",
            "000852.SH": "INDEX-CSI1000",
            "399006.SZ": "INDEX-CHINEXT",
            "000688.SH": "INDEX-STAR50",
        }
        if agent_id == "market_news_agent" and index_code in index_tokens:
            token = index_tokens[index_code]
            for document in selectable_documents:
                if token in document.entry.id.upper():
                    return document.entry
        keywords = {
            "macro_policy_agent": ("MACRO", "POLICY", "宏观"),
            "market_news_agent": ("MARKET", "INDEX", "资讯", "指数"),
            "ai_storage_industry_agent": ("AI", "STORAGE", "存储"),
            "strategy_agent": ("STRATEGY", "ALLOCATION", "策略", "配置"),
            "risk_critic_agent": ("RISK", "EVIDENCE", "风险", "证据"),
            "quant_agent": ("PREDICTION", "LABEL", "预测"),
            "cio_agent": ("EVIDENCE", "DECISION", "投委会"),
        }.get(agent_id, ())
        for document in selectable_documents:
            searchable = " ".join(
                [document.entry.id, document.entry.title, *document.entry.tags]
            ).upper()
            if any(keyword.upper() in searchable for keyword in keywords):
                return document.entry
        return selectable_documents[0].entry

    @staticmethod
    def _runtime_documents(
        documents: list[WikiDocument],
        *,
        allow_demo_fallback: bool = True,
    ) -> list[WikiDocument]:
        active_documents = [
            document for document in documents if document.entry.status.lower() == "active"
        ]
        if active_documents:
            return active_documents
        demo_documents = [
            document for document in documents if document.entry.status.lower() == "demo-only"
        ]
        if demo_documents and allow_demo_fallback:
            return demo_documents
        raise RuntimeError("Wiki catalog has no active entries available for runtime use")

    def citation(
        self,
        agent_id: str,
        *,
        section: str | None = None,
        index_code: str | None = None,
        preferred_entry_id: str | None = None,
    ) -> Citation:
        entry = self.select_for_agent(
            agent_id,
            index_code=index_code,
            preferred_entry_id=preferred_entry_id,
        )
        sections = {item.slug: item for item in entry.sections}
        chosen = sections.get(section or "") or entry.sections[0]
        return Citation(
            wiki_entry_id=entry.id,
            wiki_title=entry.title,
            wiki_version=entry.version,
            section=chosen.slug,
            quote=chosen.excerpt,
            wiki_quote=chosen.excerpt,
            content_hash=entry.content_hash,
            source_urls=entry.source_urls,
        )

    def citation_is_valid(self, citation: Citation) -> bool:
        entry = self.get(citation.wiki_entry_id, include_body=False)
        if entry is None:
            return False
        sections = {section.slug: section for section in entry.sections}
        section = sections.get(citation.section)
        if section is None:
            return False
        frozen_wiki_quote = citation.wiki_quote or (
            citation.quote if citation.evidence_item_id is None else ""
        )
        return (
            entry.title == citation.wiki_title
            and entry.version == citation.wiki_version
            and entry.content_hash == citation.content_hash
            and citation.source_urls == entry.source_urls
            and frozen_wiki_quote == section.excerpt
        )

    @staticmethod
    def _fallback_document(*, include_body: bool) -> WikiDocument:
        body = (
            "<!-- section:methodology -->\n# 演示方法\n"
            "此内置条目只用于全新安装的离线演示；正式决策必须引用仓库 Wiki。"
        )
        digest = hashlib.sha256(body.encode()).hexdigest()
        return WikiDocument(
            entry=WikiEntryRead(
                id="VC-WIKI-DEMO-METHODOLOGY",
                title="离线演示方法",
                version="0.1.0",
                updated_at=None,
                published_at=None,
                status="demo-only",
                owners=["forecast-loop"],
                tags=["demo"],
                source_urls=[],
                sections=_sections(body),
                content_hash=digest,
                body=body if include_body else None,
            ),
            path=None,
        )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]


class FrozenWikiCatalog(WikiCatalog):
    """In-memory Wiki view used for the whole lifetime of one committee run."""

    def __init__(self, entries: list[WikiEntryRead | dict[str, Any]]) -> None:
        self.path = Path(".")
        self.demo_path = None
        self._entries = tuple(
            WikiEntryRead.model_validate(entry).model_copy(deep=True) for entry in entries
        )

    def documents(self, *, include_body: bool = False) -> list[WikiDocument]:
        return [
            WikiDocument(
                entry=entry.model_copy(
                    deep=True,
                    update={"body": entry.body if include_body else None},
                ),
                path=None,
            )
            for entry in self._entries
        ]

    def freeze(
        self,
        *,
        allow_demo_fallback: bool = True,
        cutoff: datetime | None = None,
    ) -> FrozenWikiCatalog:
        if not allow_demo_fallback and any(
            entry.status.lower() != "active" for entry in self._entries
        ):
            raise RuntimeError("Wiki catalog has no active entries available for runtime use")
        if cutoff is not None:
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                raise RuntimeError("Wiki cutoff must be timezone-aware")
            visible = [
                entry
                for entry in self._entries
                if entry.published_at is not None and entry.published_at <= cutoff
            ]
            if not visible:
                invalid = [
                    entry.id
                    for entry in self._entries
                    if entry.published_at is None or entry.published_at > cutoff
                ]
                raise RuntimeError(
                    "Live Wiki entries are unpublished or newer than the evidence cutoff: "
                    + ", ".join(sorted(invalid))
                )
            return FrozenWikiCatalog(list(visible))
        return self

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            entry.model_dump(mode="json")
            for entry in (document.entry for document in self.documents(include_body=True))
        ]
