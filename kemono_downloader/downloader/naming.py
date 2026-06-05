from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .models import DateNamingMode, FileItem, PostItem


IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"}


def sanitize_filename(value: str, max_length: int = 150) -> str:
    value = re.sub(r'[\\/:*?"<>|]', "_", value or "untitled")
    value = "".join(c for c in value if unicodedata.category(c)[0] != "C")
    value = value.rstrip(" .")
    if not value:
        value = "untitled"
    return value[:max_length].rstrip(" .") or "untitled"


def post_folder_name(post: PostItem, mode: DateNamingMode) -> str:
    title = sanitize_filename(post.title)
    if mode == DateNamingMode.PREFIX:
        return sanitize_filename(f"{post.day}_{title}")
    if mode == DateNamingMode.SUFFIX:
        return sanitize_filename(f"{title}_{post.day}")
    return title


def rename_images(files: list[FileItem]) -> list[FileItem]:
    images = [item for item in files if item.kind == "image"]
    digits = max(2, len(str(len(images)))) if images else 2
    image_index = 0
    renamed: list[FileItem] = []
    used_names: set[str] = set()

    for item in files:
        name = sanitize_filename(item.name)
        if item.kind == "image":
            image_index += 1
            ext = Path(name).suffix.lower() or ".jpg"
            name = f"{image_index:0{digits}d}{ext}"
        name = unique_name(name, used_names)
        renamed.append(FileItem(url=item.url, name=name, kind=item.kind, content=item.content))
    return renamed


def unique_name(name: str, used: set[str]) -> str:
    candidate = sanitize_filename(name)
    if candidate not in used:
        used.add(candidate)
        return candidate

    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    index = 1
    while True:
        next_name = f"{stem}_{index}{suffix}"
        if next_name not in used:
            used.add(next_name)
            return next_name
        index += 1


def is_image_name(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in IMAGE_EXTS

