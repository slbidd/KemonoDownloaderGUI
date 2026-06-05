from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DownloadTask, PostItem


def app_state_dir() -> Path:
    if platform.system() == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        path = Path(root) / "KemonoDownloader" / "state"
    else:
        path = Path.home() / ".config" / "KemonoDownloader" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


class StateStore:
    def __init__(self, creator_key: str, root: Path | None = None):
        safe_key = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in creator_key)
        self.path = (root or app_state_dir()) / f"{safe_key}.json"
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "posts": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "posts": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_post_finished(self, post: PostItem) -> bool:
        return self.data.get("posts", {}).get(post.id, {}).get("status") == "finished"

    def is_file_finished(self, task: DownloadTask) -> bool:
        post = self.data.get("posts", {}).get(task.post.id, {})
        file_state = post.get("files", {}).get(task.file.name, {})
        return file_state.get("status") == "finished" and task.save_path.exists()

    def mark_file(self, task: DownloadTask, status: str, error: str | None = None) -> None:
        post_state = self._post_state(task.post)
        file_state = {
            "status": status,
            "url": task.file.url,
            "path": str(task.save_path),
            "updated_at": now_text(),
        }
        if error:
            file_state["error"] = error
        post_state.setdefault("files", {})[task.file.name] = file_state
        self.save()

    def mark_post(self, post: PostItem, status: str) -> None:
        post_state = self._post_state(post)
        post_state["status"] = status
        post_state["updated_at"] = now_text()
        self.save()

    def _post_state(self, post: PostItem) -> dict[str, Any]:
        posts = self.data.setdefault("posts", {})
        return posts.setdefault(
            post.id,
            {
                "status": "pending",
                "title": post.title,
                "url": post.url,
                "published": post.published,
                "files": {},
            },
        )


def now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

