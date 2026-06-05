from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from kemono_downloader.api.kemono import KemonoApi, normalize_creator_path
from kemono_downloader.config import DEFAULT_CONCURRENCY
from kemono_downloader.downloader.engine import DownloadEngine
from kemono_downloader.downloader.models import DateNamingMode, DownloadEvent
from kemono_downloader.downloader.service import DownloadService
from kemono_downloader.downloader.state import StateStore


FAILED_EXPORT_NAME = "failed_downloads.txt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kemono Downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="列出创作者帖子")
    list_cmd.add_argument("url", help="Kemono 创作者 URL")
    list_cmd.add_argument("--limit", type=int, default=0, help="只显示前 N 条")

    download_cmd = sub.add_parser("download", help="下载创作者帖子")
    download_cmd.add_argument("url", help="Kemono 创作者 URL")
    download_cmd.add_argument("-o", "--output", required=True, help="保存目录")
    download_cmd.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发数")
    download_cmd.add_argument("--ids", default="", help="只下载指定帖子 ID，逗号分隔")
    download_cmd.add_argument("--limit", type=int, default=0, help="只下载前 N 条，用于测试")
    return parser


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download" and args.concurrency <= 0:
        print("并发数必须大于 0")
        return 1

    api = KemonoApi()
    service = DownloadService(api)

    if args.command == "list":
        posts = await service.list_posts(args.url)
        if args.limit:
            posts = posts[: args.limit]
        for post in posts:
            print(f"{post.id}\t{post.day}\t{post.title}")
        print(f"共 {len(posts)} 条")
        return 0

    posts = await service.list_posts(args.url)
    selected_ids = parse_ids(args.ids)
    if selected_ids:
        posts = [post for post in posts if post.id in selected_ids]
    if args.limit:
        posts = posts[: args.limit]

    if not posts:
        print("没有可下载的帖子")
        return 1

    naming_mode = DateNamingMode.PREFIX
    output_dir = Path(args.output)
    failed_export_path = output_dir / FAILED_EXPORT_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    if failed_export_path.exists():
        failed_export_path.unlink()
    print(f"准备流式处理 {len(posts)} 条帖子")

    state = StateStore(f"kemono_{normalize_creator_path(args.url)}")
    engine = DownloadEngine(concurrency=args.concurrency)

    async def task_batches():
        async for batch in service.iter_post_task_batches(posts, output_dir, naming_mode):
            if batch:
                post = batch[0].post
                print(f"\n已整理: {post.title} ({len(batch)} 个文件任务)")
            yield batch

    def on_event(event: DownloadEvent) -> None:
        if event.type == "tasks_added":
            print(f"\n{event.message}")
        elif event.type == "progress":
            print(f"\r进度 {event.done or 0}/{event.total or 0} 失败 {event.failed or 0}", end="")
        elif event.type == "file_failed":
            append_failed_export(failed_export_path, event)
            print(f"\n失败: {event.file_name} - {event.message}")
        elif event.type == "finished":
            print(f"\n{event.message}")
            if failed_export_path.exists():
                print(f"失败记录已导出: {failed_export_path}")
        elif event.type == "cancelled":
            print(f"\n{event.message}")

    await engine.run_stream(task_batches(), state, on_event)
    return 0


def parse_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def append_failed_export(path: Path, event: DownloadEvent) -> None:
    data = event.data or {}
    target_path = data.get("target_path") or str(event.path or "")
    target_dir = data.get("target_dir") or str(Path(target_path).parent if target_path else "")
    first_write = not path.exists()
    with path.open("a", encoding="utf-8") as file:
        if first_write:
            file.write("# Kemono Downloader failed files\n\n")
        file.write(
            "\n".join(
                [
                    f"Post: {event.post_id or ''}",
                    f"File: {event.file_name or ''}",
                    f"URL: {data.get('url') or ''}",
                    f"Target: {target_path}",
                    f"Folder: {target_dir}",
                    f"Error: {event.message}",
                    "",
                ]
            )
        )


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
