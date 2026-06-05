from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal


class DateNamingMode(Enum):
    NONE = "none"
    PREFIX = "prefix"
    SUFFIX = "suffix"

    @classmethod
    def from_index(cls, index: int) -> "DateNamingMode":
        if index == 1:
            return cls.PREFIX
        if index == 2:
            return cls.SUFFIX
        return cls.NONE


@dataclass(slots=True)
class PostItem:
    id: str
    title: str
    published: str
    url: str
    user_id: str
    service: str
    content_html: str = ""
    raw: dict[str, Any] | None = None

    @property
    def day(self) -> str:
        if not self.published:
            return "unknown"
        return self.published.split("T", 1)[0] or "unknown"


@dataclass(slots=True)
class FileItem:
    url: str
    name: str
    kind: Literal["image", "file", "text"]
    content: str | bytes | None = None


@dataclass(slots=True)
class DownloadTask:
    post: PostItem
    file: FileItem
    save_path: Path
    temp_path: Path


@dataclass(slots=True)
class DownloadRequest:
    creator_url: str
    output_dir: Path
    selected_ids: set[str] | None = None
    concurrency: int = 8
    naming_mode: DateNamingMode = DateNamingMode.PREFIX


@dataclass(slots=True)
class DownloadEvent:
    type: str
    message: str = ""
    post_id: str | None = None
    file_name: str | None = None
    path: Path | None = None
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    data: Any = None

