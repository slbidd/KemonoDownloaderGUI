from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from kemono_downloader.api.base import PlatformApi
from kemono_downloader.config import INCLUDE_OTHER_ATTACHMENTS

from .content import build_content_text
from .models import DateNamingMode, DownloadTask, FileItem, PostItem
from .naming import post_folder_name, rename_images, sanitize_filename


class DownloadService:
    def __init__(self, api: PlatformApi, include_other_attachments: bool = INCLUDE_OTHER_ATTACHMENTS):
        self.api = api
        self.include_other_attachments = include_other_attachments
        self._reserved_post_dirs: dict[str, Path] = {}

    async def list_posts(self, creator_url: str) -> list[PostItem]:
        return await self.api.list_posts(creator_url)

    async def build_tasks(
        self,
        posts: list[PostItem],
        output_dir: Path,
        naming_mode: DateNamingMode,
    ) -> list[DownloadTask]:
        output_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[DownloadTask] = []

        for post in posts:
            tasks.extend(await self.build_post_tasks(post, output_dir, naming_mode))

        return tasks

    async def iter_post_task_batches(
        self,
        posts: list[PostItem],
        output_dir: Path,
        naming_mode: DateNamingMode,
    ) -> AsyncIterator[list[DownloadTask]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for post in posts:
            yield await self.build_post_tasks(post, output_dir, naming_mode)

    async def build_post_tasks(
        self,
        post: PostItem,
        output_dir: Path,
        naming_mode: DateNamingMode,
    ) -> list[DownloadTask]:
        output_dir.mkdir(parents=True, exist_ok=True)
        files = await self.api.get_post_files(post)
        if not self.include_other_attachments:
            files = [item for item in files if item.kind == "image"]

        content_html = await self.api.get_post_content(post)
        post.content_html = content_html

        content_item = FileItem(url=post.url, name="content.txt", kind="text", content="")
        items = rename_images([content_item, *files])
        files = [item for item in items if item.kind != "text"]
        for item in items:
            if item.kind == "text":
                item.content = build_content_text(post, files, external_links=[])
                break

        post_dir = self._resolve_post_dir(output_dir, post, naming_mode)
        tasks: list[DownloadTask] = []
        for item in items:
            save_path = post_dir / sanitize_filename(item.name)
            tasks.append(
                DownloadTask(
                    post=post,
                    file=item,
                    save_path=save_path,
                    temp_path=Path(str(save_path) + ".temp"),
                )
            )
        return tasks

    def _resolve_post_dir(self, output_dir: Path, post: PostItem, naming_mode: DateNamingMode) -> Path:
        key = f"{post.service}:{post.user_id}:{post.id}"
        if key in self._reserved_post_dirs:
            return self._reserved_post_dirs[key]

        base_name = post_folder_name(post, naming_mode)
        base_path = output_dir / base_name
        candidate = base_path
        suffix = 1

        while candidate.exists() or candidate in self._reserved_post_dirs.values():
            marker = candidate / ".post_id"
            if marker.exists():
                try:
                    if marker.read_text(encoding="utf-8").strip() == post.id:
                        break
                except Exception:
                    pass
            candidate = output_dir / f"{base_name}_{suffix}"
            suffix += 1

        candidate.mkdir(parents=True, exist_ok=True)
        marker = candidate / ".post_id"
        if not marker.exists():
            marker.write_text(post.id, encoding="utf-8")
        self._reserved_post_dirs[key] = candidate
        return candidate
