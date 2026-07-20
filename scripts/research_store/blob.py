from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import BinaryIO

from .domain import BlobReference


class ContentAddressedBlobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    @staticmethod
    def _validate_hash(digest: str) -> str:
        digest = digest.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid SHA-256")
        return digest

    def path_for(self, digest: str) -> Path:
        digest = self._validate_hash(digest)
        path = self.root / digest[:2] / digest[2:4] / digest
        resolved_parent = path.parent.resolve()
        if self.root not in (resolved_parent, *resolved_parent.parents):
            raise ValueError("blob path escapes BLOB_ROOT")
        return path

    def put(self, stream: BinaryIO, mime_type: str | None = None) -> BlobReference:
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        length = 0
        fd, temporary = tempfile.mkstemp(prefix=".blob-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    part = stream.read(1024 * 1024)
                    if not part:
                        break
                    if isinstance(part, str):
                        part = part.encode()
                    digest.update(part)
                    length += len(part)
                    handle.write(part)
                handle.flush()
                os.fsync(handle.fileno())
            hexdigest = digest.hexdigest()
            target = self.path_for(hexdigest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                os.unlink(temporary)
            else:
                os.replace(temporary, target)
            if not self.verify(hexdigest):
                raise OSError("blob verification failed after write")
            return BlobReference(
                hexdigest, f"blob://sha256/{hexdigest}", length, mime_type
            )
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def open(self, digest: str) -> BinaryIO:
        return self.path_for(digest).open("rb")

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def verify(self, digest: str) -> bool:
        digest = self._validate_hash(digest)
        path = self.path_for(digest)
        if not path.is_file():
            return False
        actual = hashlib.sha256()
        with path.open("rb") as handle:
            for part in iter(lambda: handle.read(1024 * 1024), b""):
                actual.update(part)
        return actual.hexdigest() == digest
