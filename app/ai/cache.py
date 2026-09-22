"""Versioned, tenant-aware AI response cache.

A plain directory of JSON files (no Redis, no hosted cache). Keys are
content-addressed over everything that could change the answer's meaning:
gateway version, operation, prompt version, schema version, provider and
model, the normalized input, and — for candidate-side requests — the tenant.
Entries never expire by time; a version bump makes old entries unreachable.
Nothing sensitive is stored: the input hash, the parsed output and metadata.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.ai.models import GATEWAY_VERSION, AIScope

logger = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1


def cache_key(*, operation: str, prompt_version: str, schema_version: str, provider: str, model: Optional[str], scope: AIScope, tenant_id: str, input_data: dict[str, Any]) -> str:
    """Deterministic SHA-256 over the semantic identity of a request."""
    digest = hashlib.sha256()
    parts = [
        GATEWAY_VERSION,
        f"format={CACHE_FORMAT_VERSION}",
        f"op={operation}",
        f"prompt={prompt_version}",
        f"schema={schema_version}",
        f"provider={provider}",
        f"model={model or ''}",
        f"scope={scope.value}",
        # Candidate-side inputs are tenant-private; job-side inputs may be shared.
        f"tenant={tenant_id if scope is AIScope.CANDIDATE else '*'}",
    ]
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(json.dumps(input_data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    return digest.hexdigest()


class AICache:
    """Disk cache. Every I/O error is non-fatal: a broken cache is a miss."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> Optional[dict[str, Any]]:
        path = self._path(key)
        try:
            if not path.is_file():
                self.misses += 1
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("Unreadable AI cache entry %s; treating as a miss", path.name)
            self.misses += 1
            return None
        if not isinstance(entry, dict) or entry.get("format") != CACHE_FORMAT_VERSION or entry.get("key") != key:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, *, output: Any, provider: str, model: Optional[str], operation: str, prompt_version: str, usage: dict[str, Any]) -> None:
        path = self._path(key)
        entry = {"format": CACHE_FORMAT_VERSION, "key": key, "gateway_version": GATEWAY_VERSION, "operation": operation, "prompt_version": prompt_version, "provider": provider, "model": model, "output": output, "usage": usage}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            self.writes += 1
        except OSError:
            logger.debug("Could not write AI cache entry; continuing without cache")

    def clear(self) -> int:
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def count(self) -> int:
        return sum(1 for _ in self.root.rglob("*.json")) if self.root.exists() else 0


class MemoryCache(AICache):
    """In-process cache for tests and for ``AI_CACHE_DIR`` unset."""

    def __init__(self):
        super().__init__(Path("."))
        self._entries: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, *, output: Any, provider: str, model: Optional[str], operation: str, prompt_version: str, usage: dict[str, Any]) -> None:
        self._entries[key] = {"format": CACHE_FORMAT_VERSION, "key": key, "gateway_version": GATEWAY_VERSION, "operation": operation, "prompt_version": prompt_version, "provider": provider, "model": model, "output": output, "usage": usage}
        self.writes += 1

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        return n

    def count(self) -> int:
        return len(self._entries)
