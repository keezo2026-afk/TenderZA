"""Content-hash-keyed object storage (Blueprint §5.1, §18).

The same bulletin posted on two pages is stored ONCE: the key is derived
from the SHA-256 of the bytes (``docs/ab/cd/abcd....pdf``), so duplicates
collapse naturally and dedupe-by-document works via key equality.

v1 backend is the local filesystem (dev + single node). The S3/MinIO
backend (§18) implements the same three methods; the interface is kept
boring on purpose so swapping is a config change, not a refactor.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_key(digest: str, suffix: str = "") -> str:
    """Fan out by hash prefix: docs/ab/cd/abcdef....pdf"""
    suffix = suffix if not suffix or suffix.startswith(".") else f".{suffix}"
    return f"docs/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


@dataclass
class StoredObject:
    key: str
    digest: str
    size: int
    already_existed: bool


class ObjectStore:
    """Filesystem object store keyed by content hash."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, data: bytes, *, suffix: str = "") -> StoredObject:
        digest = content_hash(data)
        key = object_key(digest, suffix)
        path = self.root / key
        existed = path.exists()
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.rename(path)  # atomic-ish: no torn objects on crash
        return StoredObject(key=key, digest=digest, size=len(data),
                            already_existed=existed)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    # Extracted text is cached alongside the object (§6: "OCR output is
    # cached as extracted text alongside the stored object").
    def put_text(self, digest: str, text: str) -> str:
        key = object_key(digest, ".txt")
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return key

    def get_text(self, digest: str) -> str | None:
        path = self.root / object_key(digest, ".txt")
        return path.read_text() if path.exists() else None
