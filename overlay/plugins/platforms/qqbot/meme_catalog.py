"""Reviewed local meme entries; model output never becomes a filesystem path."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MAX_CATALOG_BYTES = 262_144
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_AUDIENCES = frozenset({"group", "dm", "intimate_dm"})


def local_path(value: str | Path) -> Path:
    text = str(value)
    if not text or text.startswith(("\\", "//")) or "\x00" in text:
        raise ValueError("Local path required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("Absolute local path required")
    resolved = path.resolve(strict=True)
    if str(resolved).startswith(("\\", "//")):
        raise ValueError("Network path denied")
    return resolved


def _strings(value, *, maximum=8) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value or len(value) > maximum
            or any(not isinstance(x, str) or not x.strip() or len(x) > 240 for x in value)):
        raise ValueError("Invalid catalog text list")
    return tuple(value)


@dataclass(frozen=True)
class MemeEntry:
    id: str
    relative_path: str
    sha256: str
    description: str
    use_when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    audiences: frozenset[str]
    enabled: bool
    reviewed: bool

    @classmethod
    def parse(cls, item: dict) -> MemeEntry:
        if not isinstance(item, dict):
            raise ValueError("Invalid catalog entry")
        identifier, relative, digest = (item.get(k) for k in ("id", "relative_path", "sha256"))
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError("Invalid meme ID")
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise ValueError("Invalid meme digest")
        if (not isinstance(relative, str) or not relative or len(relative) > 500
                or any(c in relative for c in ("\\", ":", "\x00"))):
            raise ValueError("Invalid relative meme path")
        parts = relative.split("/")
        if any(p in {"", ".", ".."} for p in parts) or PurePosixPath(relative).is_absolute():
            raise ValueError("Meme path must stay below the catalog root")
        if PurePosixPath(relative).suffix.lower() != ".gif":
            raise ValueError("Only reviewed GIF memes are supported")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 400:
            raise ValueError("Invalid meme description")
        audiences = frozenset(_strings(item.get("audiences"), maximum=3))
        if not audiences <= _AUDIENCES:
            raise ValueError("Invalid meme audience")
        if any(type(item.get(k)) is not bool for k in ("enabled", "reviewed")):
            raise ValueError("Explicit catalog flags required")
        return cls(identifier, relative, digest, description, _strings(item.get("use_when")),
                   _strings(item.get("avoid_when")), audiences, item["enabled"], item["reviewed"])

    def prompt_data(self) -> dict:
        return {"id": self.id, "description": self.description,
                "use_when": list(self.use_when), "avoid_when": list(self.avoid_when)}


@dataclass(frozen=True)
class MemeCatalog:
    root: Path
    entries: tuple[MemeEntry, ...]

    @classmethod
    def load(cls, root: str | Path, catalog_path: str | Path) -> MemeCatalog:
        base, path = local_path(root), local_path(catalog_path)
        if not base.is_dir():
            raise ValueError("Meme root is not a directory")
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CATALOG_BYTES + 1)
        if len(raw) > _MAX_CATALOG_BYTES:
            raise ValueError("Catalog too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported meme catalog")
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 200:
            raise ValueError("Invalid catalog items")
        entries = tuple(MemeEntry.parse(item) for item in items)
        if len({entry.id for entry in entries}) != len(entries):
            raise ValueError("Duplicate meme IDs")
        return cls(base, entries)

    def candidates(self, audiences: frozenset[str], excluded=()) -> tuple[MemeEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled and entry.reviewed
                     and entry.audiences & audiences and entry.id not in excluded)[:20]

    def snapshot(self, entry: MemeEntry, directory: Path) -> Path:
        """Copy verified bytes, not a mutable original path, into a one-send artifact."""
        if entry not in self.entries or not (entry.enabled and entry.reviewed):
            raise ValueError("Unreviewed meme")
        path = local_path(self.root / entry.relative_path)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Meme resolves outside its root")
        with path.open("rb") as stream:
            raw = stream.read(_MAX_IMAGE_BYTES + 1)
        if len(raw) > _MAX_IMAGE_BYTES or raw[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ValueError("Invalid or oversized GIF")
        if hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise ValueError("Meme changed since review")
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="meme-", suffix=".gif", dir=directory)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(raw)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)
