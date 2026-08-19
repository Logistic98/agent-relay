from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from core.exceptions import WorkspaceDeniedError

_SECRET_PATTERNS: list[tuple[re.Pattern[str], bool]] = [
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}\b"), False),
    (re.compile(r"(?i)(bot)\d{8,12}:[A-Za-z0-9_-]{20,}"), True),
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"), False),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), True),
    (re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"), True),
]


def redact_text(text: str, *, limit: int | None = None) -> str:
    value = text
    for pattern, preserve_prefix in _SECRET_PATTERNS:
        if preserve_prefix:
            value = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    if limit is not None and len(value) > limit:
        value = value[: max(0, limit - 15)] + "…[truncated]"
    return value


def resolve_workspace(requested: str | Path, allowed_roots: list[Path]) -> Path:
    try:
        workspace = Path(requested).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceDeniedError("工作区不存在或无法解析") from exc
    if not workspace.is_dir():
        raise WorkspaceDeniedError("工作区必须是目录")

    roots: list[Path] = []
    for root in allowed_roots:
        try:
            roots.append(root.expanduser().resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise WorkspaceDeniedError(f"允许的工作区根目录无效：{root}") from exc
    if not any(workspace == root or workspace.is_relative_to(root) for root in roots):
        raise WorkspaceDeniedError("工作区不在 WORKSPACE_ROOTS 白名单内")
    return workspace


def first_unsafe_path(path: Path) -> Path | None:
    starts = [path.absolute()]
    try:
        starts.append(path.resolve(strict=True))
    except OSError:
        return path
    seen: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                mode = candidate.stat().st_mode
            except OSError:
                return candidate
            if mode & stat.S_IWOTH:
                sticky_directory = stat.S_ISDIR(mode) and bool(mode & stat.S_ISVTX)
                if not sticky_directory:
                    return candidate
    return None


def safe_search_path(value: str) -> str:
    entries: list[str] = []
    for raw_entry in value.split(os.pathsep):
        candidate = Path(raw_entry)
        if not raw_entry or not candidate.is_absolute() or not candidate.is_dir():
            continue
        try:
            if candidate.stat().st_mode & stat.S_IWOTH:
                continue
        except OSError:
            continue
        if first_unsafe_path(candidate) is not None:
            continue
        if raw_entry not in entries:
            entries.append(raw_entry)
    return os.pathsep.join(entries) or "/usr/bin:/bin"


def sanitized_subprocess_env(allowlist: list[str], source: Mapping[str, str] | None = None) -> dict[str, str]:
    parent = source if source is not None else os.environ
    result = {name: parent[name] for name in allowlist if name in parent}
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    result["PATH"] = safe_search_path(result["PATH"])
    result["NO_COLOR"] = "1"
    result["TERM"] = result.get("TERM", "dumb")
    return result
