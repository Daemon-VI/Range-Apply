"""Local, tenant-isolated, immutable artifact storage.

    <documents_root>/<tenant>/<candidate-opportunity>/<preparation>/resume-v1.pdf

Every path component is validated against a strict identifier pattern, the
final path must resolve inside the root, and an existing file is never
overwritten. Files are written atomically (temp file in the same directory,
then rename) and hashed with SHA-256.
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from app.config import settings
from app.core.errors import ValidationFailed
from app.documents.models import EXTENSION, MAGIC, DocumentFormat

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ARTIFACT_NAMES = {"RESUME": "resume", "COVER_LETTER": "cover-letter"}


class StorageError(ValidationFailed):
    pass


def safe_component(value: str, what: str) -> str:
    """A single path component: identifier characters only, no traversal."""
    text = (value or "").strip()
    if not _IDENTIFIER.match(text) or ".." in text or text in (".", "..") or os.sep in text or "/" in text:
        raise StorageError(f"unsafe {what} for a storage path: {text[:40]!r}")
    return text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or settings.documents_root).expanduser().resolve()

    def relative_path(self, tenant_id: str, candidate_opportunity_id: str, preparation_id: str, artifact_type: str, fmt: DocumentFormat, version: int) -> str:
        name = _ARTIFACT_NAMES.get(artifact_type)
        if name is None:
            raise StorageError(f"unknown artifact type {artifact_type!r}")
        if version < 1:
            raise StorageError("version must be >= 1")
        parts = (
            safe_component(tenant_id, "tenant id"),
            safe_component(candidate_opportunity_id, "candidate opportunity id"),
            safe_component(preparation_id, "preparation id"),
            f"{name}-v{int(version)}.{EXTENSION[fmt]}",
        )
        return "/".join(parts)

    def absolute(self, relative_path: str) -> Path:
        """Resolve a stored relative path; refuse anything escaping the root."""
        if not relative_path or relative_path.startswith(("/", "\\")) or ":" in relative_path:
            raise StorageError("artifact path must be relative")
        for part in relative_path.split("/"):
            if part in ("", ".", "..") or "\\" in part:
                raise StorageError("artifact path contains an unsafe component")
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("artifact path escapes the documents root") from exc
        return candidate

    def write_immutable(self, relative_path: str, data: bytes) -> Path:
        """Write once. An existing file is never overwritten (immutability)."""
        path = self.absolute(relative_path)
        if path.exists():
            raise StorageError(f"artifact already exists: {relative_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    def verify(self, relative_path: str, expected_hash: str, expected_size: int, fmt: DocumentFormat, max_bytes: Optional[int] = None) -> list[str]:
        """Problems with the stored file (empty list = intact)."""
        problems: list[str] = []
        try:
            path = self.absolute(relative_path)
        except StorageError as exc:
            return [exc.message]
        if not path.is_file():
            return ["file missing"]
        size = path.stat().st_size
        if size == 0:
            problems.append("file is empty")
        if size != expected_size:
            problems.append(f"size {size} != recorded {expected_size}")
        limit = max_bytes or settings.documents_max_bytes
        if size > limit:
            problems.append(f"size {size} exceeds the {limit}-byte bound")
        with open(path, "rb") as handle:
            head = handle.read(8)
        if not head.startswith(MAGIC[fmt]):
            problems.append(f"file is not a {fmt.value}")
        if size and sha256_file(path) != expected_hash:
            problems.append("SHA-256 does not match the recorded hash")
        return problems
