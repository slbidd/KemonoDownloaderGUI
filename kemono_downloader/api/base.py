from __future__ import annotations

from typing import Protocol

from kemono_downloader.downloader.models import FileItem, PostItem


class PlatformApi(Protocol):
    """Common source API. Other platforms can implement this interface."""

    async def list_posts(self, creator_url: str) -> list[PostItem]:
        """Return available posts for a creator/user URL."""

    async def get_post_files(self, post: PostItem) -> list[FileItem]:
        """Return downloadable files and metadata files for one post."""

    async def get_post_content(self, post: PostItem) -> str:
        """Return raw post content, usually HTML."""

