"""Small read-only JSON helper shared by the public examples."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class ExampleSourceAccessError(RuntimeError):
    """The configured example source cannot be read without crossing a boundary."""


class ExampleSourceFormatError(RuntimeError):
    """The configured example source is not a complete UTF-8 JSON document."""


def read_json_document(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Any:
    """Read one exact regular file without following a symlink."""

    configured = Path(path)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if configured.is_symlink():
        raise ExampleSourceAccessError(f"example source may not be a symlink: {configured}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(configured, flags)
    except OSError as exc:
        raise ExampleSourceAccessError(
            f"example source cannot be opened safely: {configured}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ExampleSourceAccessError(
                f"example source must be a regular file: {configured}"
            )
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise ExampleSourceAccessError(
                f"example source size must be between 1 and {max_bytes} bytes: {configured}"
            )
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise ExampleSourceAccessError(
                f"example source changed while being read: {configured}"
            )
    except OSError as exc:
        raise ExampleSourceAccessError(
            f"example source could not be read: {configured}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExampleSourceFormatError(
            f"example source is not valid UTF-8 JSON: {configured}"
        ) from exc
