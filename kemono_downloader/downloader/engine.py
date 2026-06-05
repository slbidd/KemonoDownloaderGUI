from __future__ import annotations

import asyncio
import inspect
import os
import threading
from collections import Counter
from pathlib import Path
from typing import AsyncIterable, Awaitable, Callable

import aiofiles
import aiohttp

from .models import DownloadEvent, DownloadTask
from .state import StateStore


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DOWNLOAD_HEADERS = {"User-Agent": USER_AGENT}
EventCallback = Callable[[DownloadEvent], None | Awaitable[None]]


class DownloadEngine:
    def __init__(
        self,
        concurrency: int = 8,
        retries: int = 3,
        chunk_size: int = 1024 * 1024,
        pause_event: threading.Event | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.concurrency = max(1, concurrency)
        self.retries = max(1, retries)
        self.chunk_size = chunk_size
        self.pause_event = pause_event or threading.Event()
        self.stop_event = stop_event or threading.Event()
        self.pause_event.set()

    async def run(
        self,
        tasks: list[DownloadTask],
        state_store: StateStore | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        total = len(tasks)
        done = 0
        failed = 0
        post_total = Counter(task.post.id for task in tasks)
        post_done: Counter[str] = Counter()
        post_failed: Counter[str] = Counter()
        lock = asyncio.Lock()

        await emit(on_event, DownloadEvent(type="started", total=total, done=0, failed=0))

        queue: asyncio.Queue[DownloadTask | None] = asyncio.Queue()
        for task in tasks:
            queue.put_nowait(task)
        for _ in range(self.concurrency):
            queue.put_nowait(None)

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        async with aiohttp.ClientSession(headers=DOWNLOAD_HEADERS, timeout=timeout) as session:
            workers = [
                asyncio.create_task(
                    self._worker(
                        queue,
                        session,
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                    )
                )
                for _ in range(self.concurrency)
            ]

            for worker in workers:
                worker.add_done_callback(lambda _task: None)

            await self._run_workers(
                workers,
                queue,
            )

        done = sum(post_done.values())
        failed = sum(post_failed.values())
        event_type = "cancelled" if self.stop_event.is_set() else "finished"
        await emit(
            on_event,
            DownloadEvent(
                type=event_type,
                total=total,
                done=done,
                failed=failed,
                message="已停止" if event_type == "cancelled" else "全部下载处理完成",
            ),
        )

    async def run_stream(
        self,
        task_batches: AsyncIterable[list[DownloadTask]],
        state_store: StateStore | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        post_total: Counter[str] = Counter()
        post_done: Counter[str] = Counter()
        post_failed: Counter[str] = Counter()
        lock = asyncio.Lock()
        queue: asyncio.Queue[DownloadTask | None] = asyncio.Queue()

        await emit(on_event, DownloadEvent(type="started", total=0, done=0, failed=0))

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        async with aiohttp.ClientSession(headers=DOWNLOAD_HEADERS, timeout=timeout) as session:
            workers = [
                asyncio.create_task(
                    self._worker(
                        queue,
                        session,
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                    )
                )
                for _ in range(self.concurrency)
            ]

            try:
                async for batch in task_batches:
                    if self.stop_event.is_set():
                        break
                    if not batch:
                        continue

                    async with lock:
                        for task in batch:
                            post_total[task.post.id] += 1
                        total = sum(post_total.values())
                        done = sum(post_done.values())
                        failed = sum(post_failed.values())

                    post = batch[0].post
                    await emit(
                        on_event,
                        DownloadEvent(
                            type="tasks_added",
                            post_id=post.id,
                            total=total,
                            done=done,
                            failed=failed,
                            data=batch,
                            message=f"已加入 {len(batch)} 个文件任务: {post.title}",
                        ),
                    )
                    await emit(
                        on_event,
                        DownloadEvent(type="progress", total=total, done=done, failed=failed),
                    )

                    for task in batch:
                        await queue.put(task)
            finally:
                for _ in range(self.concurrency):
                    await queue.put(None)
                await self._run_workers(workers, queue)

        done = sum(post_done.values())
        failed = sum(post_failed.values())
        total = sum(post_total.values())
        event_type = "cancelled" if self.stop_event.is_set() else "finished"
        await emit(
            on_event,
            DownloadEvent(
                type=event_type,
                total=total,
                done=done,
                failed=failed,
                message="已停止" if event_type == "cancelled" else "全部下载处理完成",
            ),
        )

    async def _run_workers(
        self,
        workers: list[asyncio.Task],
        queue: asyncio.Queue,
    ) -> None:
        try:
            await queue.join()
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(
        self,
        queue: asyncio.Queue[DownloadTask | None],
        session: aiohttp.ClientSession,
        state_store: StateStore | None,
        on_event: EventCallback | None,
        lock: asyncio.Lock,
        post_total: Counter,
        post_done: Counter,
        post_failed: Counter,
    ) -> None:
        while True:
            task = await queue.get()
            try:
                if task is None:
                    return

                await self._wait_if_paused()
                if self.stop_event.is_set():
                    await self._mark_task(
                        task,
                        "cancelled",
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                    )
                    continue

                if state_store and state_store.is_file_finished(task):
                    await self._mark_task(
                        task,
                        "skipped",
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                    )
                    continue

                if task.file.kind != "text" and task.save_path.exists() and task.save_path.stat().st_size > 0:
                    if state_store:
                        state_store.mark_file(task, "finished")
                    await self._mark_task(
                        task,
                        "skipped",
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                    )
                    continue

                await emit(
                    on_event,
                    DownloadEvent(
                        type="file_started",
                        post_id=task.post.id,
                        file_name=task.file.name,
                        path=task.save_path,
                        message=f"开始: {task.file.name}",
                    ),
                )
                await self._run_task(session, task)
                if state_store:
                    state_store.mark_file(task, "finished")
                await self._mark_task(
                    task,
                    "finished",
                    state_store,
                    on_event,
                    lock,
                    post_total,
                    post_done,
                    post_failed,
                )
            except Exception as exc:
                if task is not None and state_store:
                    state_store.mark_file(task, "failed", str(exc))
                if task is not None:
                    await self._mark_task(
                        task,
                        "failed",
                        state_store,
                        on_event,
                        lock,
                        post_total,
                        post_done,
                        post_failed,
                        error=str(exc),
                    )
            finally:
                queue.task_done()

    async def _mark_task(
        self,
        task: DownloadTask,
        status: str,
        state_store: StateStore | None,
        on_event: EventCallback | None,
        lock: asyncio.Lock,
        post_total: Counter,
        post_done: Counter,
        post_failed: Counter,
        error: str | None = None,
    ) -> None:
        async with lock:
            post_done[task.post.id] += 1
            if status == "failed":
                post_failed[task.post.id] += 1

            done = sum(post_done.values())
            failed = sum(post_failed.values())
            total = sum(post_total.values())
            event_type = {
                "finished": "file_finished",
                "skipped": "file_skipped",
                "failed": "file_failed",
                "cancelled": "file_cancelled",
            }.get(status, "file_finished")

            await emit(
                on_event,
                DownloadEvent(
                    type=event_type,
                    post_id=task.post.id,
                    file_name=task.file.name,
                    path=task.save_path,
                    done=done,
                    total=total,
                    failed=failed,
                    data={
                        "url": task.file.url,
                        "target_dir": str(task.save_path.parent),
                        "target_path": str(task.save_path),
                    },
                    message=error or f"{status}: {task.file.name}",
                ),
            )
            await emit(
                on_event,
                DownloadEvent(type="progress", total=total, done=done, failed=failed),
            )

            if post_done[task.post.id] >= post_total[task.post.id]:
                post_status = "failed" if post_failed[task.post.id] else "finished"
                if state_store:
                    state_store.mark_post(task.post, post_status)
                await emit(
                    on_event,
                    DownloadEvent(
                        type="post_finished",
                        post_id=task.post.id,
                        failed=post_failed[task.post.id],
                        message=f"帖子完成: {task.post.title}",
                    ),
                )

    async def _run_task(self, session: aiohttp.ClientSession, task: DownloadTask) -> None:
        task.save_path.parent.mkdir(parents=True, exist_ok=True)
        if task.file.kind == "text":
            data = task.file.content or ""
            if isinstance(data, bytes):
                await write_bytes_atomic(task.save_path, task.temp_path, data)
            else:
                await write_text_atomic(task.save_path, task.temp_path, str(data))
            return

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            await self._wait_if_paused()
            if self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            try:
                await self._download_file(session, task)
                return
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(min(3, attempt))
        raise RuntimeError(str(last_error) if last_error else "下载失败")

    async def _download_file(self, session: aiohttp.ClientSession, task: DownloadTask) -> None:
        existing_size = task.temp_path.stat().st_size if task.temp_path.exists() else 0
        headers = {"Range": f"bytes={existing_size}-"} if existing_size else {}

        async with session.get(task.file.url, headers=headers) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"HTTP {resp.status}")

            if existing_size and resp.status == 200:
                existing_size = 0
                mode = "wb"
            else:
                mode = "ab" if existing_size else "wb"

            async with aiofiles.open(task.temp_path, mode) as file:
                async for chunk in resp.content.iter_chunked(self.chunk_size):
                    await self._wait_if_paused()
                    if self.stop_event.is_set():
                        raise RuntimeError("任务已停止")
                    if chunk:
                        await file.write(chunk)

        os.replace(task.temp_path, task.save_path)

    async def _wait_if_paused(self) -> None:
        while not self.pause_event.is_set():
            if self.stop_event.is_set():
                return
            await asyncio.sleep(0.2)


async def write_text_atomic(path: Path, temp_path: Path, text: str) -> None:
    async with aiofiles.open(temp_path, "w", encoding="utf-8") as file:
        await file.write(text)
    os.replace(temp_path, path)


async def write_bytes_atomic(path: Path, temp_path: Path, data: bytes) -> None:
    async with aiofiles.open(temp_path, "wb") as file:
        await file.write(data)
    os.replace(temp_path, path)


async def emit(callback: EventCallback | None, event: DownloadEvent) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result
