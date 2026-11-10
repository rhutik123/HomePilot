import os
import hashlib
from abc import ABC, abstractmethod
from typing import BinaryIO, Optional
from django.conf import settings
from django.core.files.storage import default_storage, FileSystemStorage


class BaseDocumentStorage(ABC):
    """
    Abstract Storage interface for HomePilot documents.
    Enables swapping between secure local filesystem storage and cloud S3/GCS
    without altering document or bill business logic.
    """
    @abstractmethod
    def save(self, name: str, content: BinaryIO) -> str:
        """Saves file content and returns the internal storage path/key."""
        pass

    @abstractmethod
    def open(self, path: str, mode: str = "rb") -> BinaryIO:
        """Opens and returns a binary stream of the stored file."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Returns True if the file exists in storage."""
        pass

    @abstractmethod
    def delete(self, path: str) -> bool:
        """Deletes a file from storage."""
        pass

    @abstractmethod
    def compute_sha256(self, content_or_stream) -> str:
        """Computes SHA-256 hash of file content for duplicate detection."""
        pass


class LocalSecureDocumentStorage(BaseDocumentStorage):
    """
    Local filesystem implementation of document storage.
    Files are stored in a protected folder and served exclusively
    through authenticated streaming endpoints.
    """
    def __init__(self, root_dir: Optional[str] = None):
        self.root_dir = root_dir or getattr(settings, "PROTECTED_MEDIA_ROOT", str(settings.MEDIA_ROOT / "documents"))
        os.makedirs(self.root_dir, exist_ok=True)
        self._storage = FileSystemStorage(location=self.root_dir)

    def save(self, name: str, content: BinaryIO) -> str:
        if hasattr(content, "seek"):
            content.seek(0)
        saved_name = self._storage.save(name, content)
        if hasattr(content, "seek"):
            content.seek(0)
        return saved_name

    def open(self, path: str, mode: str = "rb"):
        return self._storage.open(path, mode=mode)

    def exists(self, path: str) -> bool:
        return self._storage.exists(path)

    def delete(self, path: str) -> bool:
        if self.exists(path):
            self._storage.delete(path)
            return True
        return False

    def compute_sha256(self, content_or_stream) -> str:
        sha = hashlib.sha256()
        if hasattr(content_or_stream, "read"):
            pos = content_or_stream.tell() if hasattr(content_or_stream, "tell") else 0
            if hasattr(content_or_stream, "seek"):
                content_or_stream.seek(0)
            while chunk := content_or_stream.read(65536):
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                sha.update(chunk)
            if hasattr(content_or_stream, "seek"):
                content_or_stream.seek(pos)
        elif isinstance(content_or_stream, bytes):
            sha.update(content_or_stream)
        elif isinstance(content_or_stream, str):
            sha.update(content_or_stream.encode("utf-8"))
        return sha.hexdigest()


_storage_singleton: Optional[BaseDocumentStorage] = None


def get_document_storage() -> BaseDocumentStorage:
    """Factory returning configured document storage backend."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = LocalSecureDocumentStorage()
    return _storage_singleton
