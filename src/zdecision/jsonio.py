"""Canonical JSON encoding and atomic filesystem writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def canonical_json_bytes(value: object) -> bytes:
    """Encode JSON deterministically as UTF-8 with one trailing newline."""

    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def atomic_write_json(path: Path, value: object) -> None:
    """Replace *path* with one durable canonical JSON document."""

    atomic_write_bytes(path, canonical_json_bytes(value))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace *path* with exact durable bytes."""

    if not isinstance(content, bytes):
        raise TypeError("Atomic file content must be bytes")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_create_json(path: Path, value: object) -> bool:
    """Atomically create *path* without ever replacing an existing object."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    created = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        created = True
        _fsync_directory(path.parent)
        return True
    finally:
        temporary_path.unlink(missing_ok=True)
        if created:
            _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
