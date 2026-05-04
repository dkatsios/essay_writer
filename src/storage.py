"""Artifact storage with pluggable backends.

Every pipeline run stores artifacts under a key prefix like
``runs/<job_id>/``.  Three concrete implementations share the same
interface:

* ``RunStorage`` — Cloudflare R2 / S3-compatible (production default)
* ``LocalRunStorage`` — local filesystem (for development without R2)
* ``MemoryRunStorage`` — in-memory dict (unit tests)

The ``create_run_storage()`` factory inspects ``StorageConfig.backend``
(``"r2"`` or ``"local"``) and returns the right implementation.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from config.settings import StorageConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class BaseRunStorage(ABC):
    """Common interface for all run-storage backends."""

    @property
    @abstractmethod
    def prefix(self) -> str: ...

    @abstractmethod
    def read_bytes(self, subpath: str) -> bytes: ...

    def read_text(self, subpath: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(subpath).decode(encoding)

    @abstractmethod
    def write_bytes(self, subpath: str, data: bytes) -> None: ...

    def write_text(self, subpath: str, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(subpath, text.encode(encoding))

    @abstractmethod
    def exists(self, subpath: str) -> bool: ...

    @abstractmethod
    def file_size(self, subpath: str) -> int: ...

    @abstractmethod
    def list_files(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def list_dir(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def delete(self, subpath: str) -> None: ...

    @abstractmethod
    def delete_all(self) -> int: ...

    def iter_all_files(self) -> list[str]:
        return self.list_files()


# ---------------------------------------------------------------------------
# R2 / S3 backend
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_cached_client: object | None = None
_cached_client_key: tuple | None = None


def get_s3_client(config: StorageConfig):
    """Return a shared boto3 S3 client for the configured R2 endpoint."""
    global _cached_client, _cached_client_key

    import boto3
    from botocore.config import Config as BotoConfig

    key = (
        config.r2_endpoint_url,
        config.r2_access_key_id,
        config.r2_secret_access_key,
    )
    with _client_lock:
        if _cached_client is not None and _cached_client_key == key:
            return _cached_client
        client = boto3.client(
            "s3",
            endpoint_url=config.r2_endpoint_url,
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name="auto",
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
            ),
        )
        _cached_client = client
        _cached_client_key = key
        return client


class RunStorage(BaseRunStorage):
    """S3/R2-backed artifact storage for a single pipeline run."""

    def __init__(self, client, bucket: str, prefix: str) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"

    @property
    def prefix(self) -> str:
        return self._prefix

    def _key(self, subpath: str) -> str:
        return self._prefix + subpath

    # -- read ------------------------------------------------------------------

    def read_bytes(self, subpath: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._key(subpath))
            return resp["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise FileNotFoundError(subpath) from exc
            raise

    # -- write -----------------------------------------------------------------

    def write_bytes(self, subpath: str, data: bytes) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(subpath),
            Body=data,
        )

    # -- query -----------------------------------------------------------------

    def exists(self, subpath: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(subpath))
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def file_size(self, subpath: str) -> int:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=self._key(subpath))
            return resp["ContentLength"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                raise FileNotFoundError(subpath) from exc
            raise

    def list_files(self, prefix: str = "") -> list[str]:
        full_prefix = self._key(prefix)
        files: list[str] = []
        continuation_token = None
        while True:
            kwargs: dict = {
                "Bucket": self._bucket,
                "Prefix": full_prefix,
                "MaxKeys": 1000,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                key: str = obj["Key"]
                files.append(key[len(self._prefix) :])
            if resp.get("IsTruncated"):
                continuation_token = resp["NextContinuationToken"]
            else:
                break
        return sorted(files)

    def list_dir(self, prefix: str = "") -> list[str]:
        full_prefix = self._key(prefix)
        if prefix and not full_prefix.endswith("/"):
            full_prefix += "/"
        files: list[str] = []
        resp = self._client.list_objects_v2(
            Bucket=self._bucket,
            Prefix=full_prefix,
            Delimiter="/",
            MaxKeys=1000,
        )
        for obj in resp.get("Contents", []):
            key: str = obj["Key"]
            relative = key[len(full_prefix) :]
            if relative:
                files.append(relative)
        return sorted(files)

    # -- delete ----------------------------------------------------------------

    def delete(self, subpath: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._key(subpath))

    def delete_all(self) -> int:
        deleted = 0
        all_files = self.list_files()
        batch_size = 1000
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i : i + batch_size]
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={
                    "Objects": [{"Key": self._key(f)} for f in batch],
                    "Quiet": True,
                },
            )
            deleted += len(batch)
        return deleted


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------


class LocalRunStorage(BaseRunStorage):
    """Filesystem-backed artifact storage for local development."""

    def __init__(self, base_dir: str | Path, prefix: str) -> None:
        self._base = Path(base_dir).resolve()
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"
        self._root = self._base / self._prefix.rstrip("/")

    @property
    def prefix(self) -> str:
        return self._prefix

    def _path(self, subpath: str) -> Path:
        return self._root / subpath

    # -- read ------------------------------------------------------------------

    def read_bytes(self, subpath: str) -> bytes:
        p = self._path(subpath)
        if not p.is_file():
            raise FileNotFoundError(subpath)
        return p.read_bytes()

    # -- write -----------------------------------------------------------------

    def write_bytes(self, subpath: str, data: bytes) -> None:
        p = self._path(subpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # -- query -----------------------------------------------------------------

    def exists(self, subpath: str) -> bool:
        return self._path(subpath).is_file()

    def file_size(self, subpath: str) -> int:
        p = self._path(subpath)
        if not p.is_file():
            raise FileNotFoundError(subpath)
        return p.stat().st_size

    def list_files(self, prefix: str = "") -> list[str]:
        search_dir = self._root / prefix if prefix else self._root
        if not search_dir.is_dir():
            return []
        root_str = str(self._root) + os.sep
        files: list[str] = []
        for p in search_dir.rglob("*"):
            if p.is_file():
                rel = str(p).replace(root_str, "", 1)
                files.append(rel.replace(os.sep, "/"))
        return sorted(files)

    def list_dir(self, prefix: str = "") -> list[str]:
        search_dir = self._root / prefix if prefix else self._root
        if not search_dir.is_dir():
            return []
        return sorted(p.name for p in search_dir.iterdir() if p.is_file())

    # -- delete ----------------------------------------------------------------

    def delete(self, subpath: str) -> None:
        p = self._path(subpath)
        if p.is_file():
            p.unlink()

    def delete_all(self) -> int:
        if not self._root.is_dir():
            return 0
        count = sum(1 for p in self._root.rglob("*") if p.is_file())
        shutil.rmtree(self._root)
        return count


# ---------------------------------------------------------------------------
# In-memory backend (tests)
# ---------------------------------------------------------------------------


class MemoryRunStorage(BaseRunStorage):
    """In-memory dict-backed storage for tests."""

    def __init__(self, prefix: str = "test/") -> None:
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"
        self._files: dict[str, bytes] = {}

    @property
    def prefix(self) -> str:
        return self._prefix

    def read_bytes(self, subpath: str) -> bytes:
        if subpath not in self._files:
            raise FileNotFoundError(subpath)
        return self._files[subpath]

    def write_bytes(self, subpath: str, data: bytes) -> None:
        self._files[subpath] = data

    def exists(self, subpath: str) -> bool:
        return subpath in self._files

    def file_size(self, subpath: str) -> int:
        if subpath not in self._files:
            raise FileNotFoundError(subpath)
        return len(self._files[subpath])

    def list_files(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._files if k.startswith(prefix))

    def list_dir(self, prefix: str = "") -> list[str]:
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        result: list[str] = []
        for key in self._files:
            if key.startswith(prefix):
                relative = key[len(prefix) :]
                if "/" not in relative:
                    result.append(relative)
        return sorted(result)

    def delete(self, subpath: str) -> None:
        self._files.pop(subpath, None)

    def delete_all(self) -> int:
        count = len(self._files)
        self._files.clear()
        return count


# ---------------------------------------------------------------------------
# Type alias + factory
# ---------------------------------------------------------------------------

AnyStorage = Union[RunStorage, LocalRunStorage, MemoryRunStorage]


def create_run_storage(
    job_id: str, config: StorageConfig | None = None
) -> RunStorage | LocalRunStorage:
    """Create a storage instance for *job_id* based on config backend setting."""
    if config is None:
        from config.settings import load_config

        config = load_config().storage

    prefix = f"{config.run_prefix}{job_id}"

    if config.backend == "local":
        return LocalRunStorage(config.local_dir, prefix)

    # Default: R2
    client = get_s3_client(config)
    return RunStorage(client, config.r2_bucket, prefix)
