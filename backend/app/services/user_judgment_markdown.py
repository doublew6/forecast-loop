"""Private, append-only Markdown projection for User Judgment records."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePath
from typing import Any
from uuid import uuid4

MAX_WIKI_BYTES = 64 * 1024
USER_JUDGMENT_SCHEMA_V1 = "vericouncil.user-judgment/v1"
USER_JUDGMENT_SCHEMA_V2 = "forecast-loop.user-judgment/v2"
USER_JUDGMENT_POLICY_V1 = "user-judgment/v1"
USER_JUDGMENT_POLICY_V2 = "user-judgment/v2"
USER_JUDGMENT_POLICY_V3 = "user-judgment/v3"


class UserJudgmentWikiError(ValueError):
    """The private User Judgment Wiki failed a path or integrity check."""


def render_user_judgment_markdown(payload: dict[str, Any], content_hash: str) -> bytes:
    """Render a canonical page using the policy sealed into the record."""

    policy_version = payload.get("policy_version")
    if policy_version == USER_JUDGMENT_POLICY_V1:
        return render_user_judgment_markdown_v1(payload, content_hash)
    if policy_version in {USER_JUDGMENT_POLICY_V2, USER_JUDGMENT_POLICY_V3}:
        return render_user_judgment_markdown_v2(payload, content_hash)
    raise UserJudgmentWikiError("Unsupported User Judgment Markdown policy version")


def user_judgment_schema_for_policy(policy_version: str) -> str:
    if policy_version == USER_JUDGMENT_POLICY_V1:
        return USER_JUDGMENT_SCHEMA_V1
    if policy_version in {USER_JUDGMENT_POLICY_V2, USER_JUDGMENT_POLICY_V3}:
        return USER_JUDGMENT_SCHEMA_V2
    raise UserJudgmentWikiError("Unsupported User Judgment policy version")


def render_user_judgment_markdown_v1(
    payload: dict[str, Any],
    content_hash: str,
) -> bytes:
    """Render the frozen v1 VeriCouncil page byte-for-byte."""

    return _render_user_judgment_markdown(
        payload,
        content_hash,
        schema=USER_JUDGMENT_SCHEMA_V1,
        brand="VeriCouncil",
    )


def render_user_judgment_markdown_v2(
    payload: dict[str, Any],
    content_hash: str,
) -> bytes:
    """Render the forecast-loop branded v2 page."""

    return _render_user_judgment_markdown(
        payload,
        content_hash,
        schema=USER_JUDGMENT_SCHEMA_V2,
        brand="forecast-loop",
    )


def _render_user_judgment_markdown(
    payload: dict[str, Any],
    content_hash: str,
    *,
    schema: str,
    brand: str,
) -> bytes:
    if payload.get("schema") != schema:
        raise UserJudgmentWikiError(
            "User Judgment schema does not match its policy version"
        )

    direction_label = "上涨" if payload["direction"] == "up" else "下跌"
    frontmatter = [
        "---",
        f"schema: {schema}",
        f"id: {_yaml_string(payload['id'])}",
        f"actor_id: {_yaml_string(payload['actor_id'])}",
        f"agent_id: {_yaml_string(payload['agent_id'])}",
        f"agent_version: {_yaml_string(payload['agent_version'])}",
        f"forecast_id: {_yaml_string(payload['forecast_id'])}",
        f"run_id: {_yaml_string(payload['run_id'])}",
        f"mode: {_yaml_string(payload['mode'])}",
        f"index_code: {_yaml_string(payload['index_code'])}",
        f"horizon: {_yaml_string(payload['horizon'])}",
        f"target_date: {_yaml_string(payload['target_date'])}",
        f"direction: {_yaml_string(payload['direction'])}",
        f"confidence_hex: {_yaml_string(payload['confidence_hex'])}",
        f"blind_attestation: {str(payload['blind_attestation']).lower()}",
        f"formal_score_eligible: {str(payload['formal_score_eligible']).lower()}",
        f"submitted_at: {_yaml_string(payload['submitted_at'])}",
        (
            f"submission_deadline: {_yaml_string(payload['submission_deadline'])}"
            if payload["submission_deadline"] is not None
            else "submission_deadline: null"
        ),
        f"policy_version: {_yaml_string(payload['policy_version'])}",
        f"run_input_hash: {_yaml_string(payload['run_input_hash'])}",
        f"forecast_input_hash: {_yaml_string(payload['forecast_input_hash'])}",
        f"content_hash: {_yaml_string(content_hash)}",
        "---",
    ]
    body = [
        "",
        f"# {payload['target_date']} · {payload['index_code']} · {payload['horizon']}",
        "",
        f"> 个人判断存档：这是不可覆盖的事前观点日志，不是 {brand} 正式、",
        "> 可被预测 Agent 引用的常青知识条目。",
        "",
        "<!-- section:prediction -->",
        "## 预测",
        "",
        f"- 方向：**{direction_label}** (`{payload['direction']}`)",
        f"- 主观置信度：**{float.fromhex(payload['confidence_hex']):.0%}**",
        (
            "- 独立性声明：提交者声明尚未查看本次委员会结论。"
            if payload["blind_attestation"]
            else "- 独立性声明：未声明盲判；该记录不进入正式影子成绩。"
        ),
        "",
        "<!-- section:rationale -->",
        "## 核心理由",
        "",
        payload["rationale"],
        "",
        "<!-- section:counter-evidence -->",
        "## 最强反方证据",
        "",
        payload["counter_evidence"],
        "",
        "<!-- section:invalidation -->",
        "## 失效条件",
        "",
        payload["invalidation_condition"],
        "",
        "<!-- section:audit -->",
        "## 审计封条",
        "",
        f"- User Judgment hash: `{content_hash}`",
        f"- Run input hash: `{payload['run_input_hash']}`",
        f"- Forecast input hash: `{payload['forecast_input_hash']}`",
        "",
    ]
    return ("\n".join([*frontmatter, *body])).encode("utf-8")


def publish_user_judgment_markdown(
    root: Path,
    relative_path: str,
    content: bytes,
) -> str:
    """Publish one file without following symlinks or overwriting a prior seal."""

    if len(content) > MAX_WIKI_BYTES:
        raise UserJudgmentWikiError("User Judgment Wiki page exceeds the size limit")
    resolved_root = _prepare_root(root)
    target = _safe_target(resolved_root, relative_path, create_parents=True)
    if target.exists() or target.is_symlink():
        raise UserJudgmentWikiError("User Judgment Wiki page already exists")

    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target, follow_symlinks=False)
        os.chmod(target, 0o400, follow_symlinks=False)
        _fsync_directory(target.parent)
    except FileExistsError as exc:
        raise UserJudgmentWikiError(
            "User Judgment Wiki page already exists"
        ) from exc
    except OSError as exc:
        raise UserJudgmentWikiError(
            "User Judgment Wiki page could not be sealed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(content).hexdigest()


def load_verified_user_judgment_markdown(
    root: Path,
    relative_path: str,
    *,
    expected_hash: str,
    expected_content: bytes | None = None,
) -> str:
    resolved_root = _prepare_root(root)
    target = _safe_target(resolved_root, relative_path, create_parents=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as exc:
        raise UserJudgmentWikiError("User Judgment Wiki page is missing") from exc
    except OSError as exc:
        raise UserJudgmentWikiError(
            "User Judgment Wiki page could not be opened safely"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise UserJudgmentWikiError(
                    "User Judgment Wiki page must be a regular file"
                )
            if before.st_size > MAX_WIKI_BYTES:
                raise UserJudgmentWikiError(
                    "User Judgment Wiki page exceeds the size limit"
                )
            content = stream.read(MAX_WIKI_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise UserJudgmentWikiError(
            "User Judgment Wiki page could not be read safely"
        ) from exc
    if len(content) > MAX_WIKI_BYTES:
        raise UserJudgmentWikiError("User Judgment Wiki page exceeds the size limit")
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or after.st_size != len(content)
    ):
        raise UserJudgmentWikiError(
            "User Judgment Wiki page changed while it was read"
        )
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        raise UserJudgmentWikiError("User Judgment Wiki artifact hash mismatch")
    if expected_content is not None and content != expected_content:
        raise UserJudgmentWikiError("User Judgment Wiki canonical content mismatch")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserJudgmentWikiError("User Judgment Wiki page is not UTF-8") from exc


def remove_user_judgment_markdown(root: Path, relative_path: str) -> None:
    """Best-effort rollback for an artifact whose database insert failed."""

    try:
        resolved_root = _prepare_root(root)
        target = _safe_target(resolved_root, relative_path, create_parents=False)
        metadata = target.lstat()
        if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            target.unlink()
            _fsync_directory(target.parent)
    except (FileNotFoundError, OSError, UserJudgmentWikiError):
        return


def _prepare_root(root: Path) -> Path:
    configured = root.expanduser()
    if configured.exists():
        if configured.is_symlink() or not configured.is_dir():
            raise UserJudgmentWikiError(
                "User Judgment Wiki root must be a real directory"
            )
    else:
        configured.mkdir(parents=True, mode=0o700)
        os.chmod(configured, 0o700)
    return configured.resolve(strict=True)


def _safe_target(
    root: Path,
    relative_path: str,
    *,
    create_parents: bool,
) -> Path:
    relative = PurePath(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise UserJudgmentWikiError("User Judgment Wiki path escapes its root")
    target = root.joinpath(*relative.parts)
    if create_parents:
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists():
                if current.is_symlink() or not current.is_dir():
                    raise UserJudgmentWikiError(
                        "User Judgment Wiki path contains a symlink"
                    )
            else:
                current.mkdir(mode=0o700)
                os.chmod(current, 0o700)
    parent = target.parent.resolve(strict=True)
    if not parent.is_relative_to(root):
        raise UserJudgmentWikiError("User Judgment Wiki path escapes its root")
    return parent / target.name


def _yaml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # The content file itself is already fsynced. Some filesystems do not
        # permit fsync on directories, so this remains a best-effort hardening.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
