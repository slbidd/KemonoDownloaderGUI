from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kemono_downloader.api.kemono import KemonoApi, normalize_creator_path
from kemono_downloader.config import DEFAULT_CONCURRENCY
from kemono_downloader.downloader.engine import DownloadEngine
from kemono_downloader.downloader.models import DateNamingMode, DownloadEvent, PostItem
from kemono_downloader.downloader.service import DownloadService
from kemono_downloader.downloader.state import StateStore


ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"
BACKGROUND_PATH = ASSET_DIR / "background.jpg"
FONT_PATH = ASSET_DIR / "YeZiGongChangTangYingHei-2.ttf"
ICON_PATH = ASSET_DIR / "icon.ico"
FAILED_EXPORT_NAME = "failed_downloads.txt"
DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
WINDOWS_APP_ID = "KemonoDownloader.PreviewImageDownloader"
ANNOUNCEMENT_URL = (
    "https://gist.githubusercontent.com/slbidd/"
    "8fc6b2357a7b5b6915ad6a2297d776ca/raw/announcement.txt"
)


class AnnouncementLoader(QObject):
    loaded = pyqtSignal(str)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            request = urllib.request.Request(
                ANNOUNCEMENT_URL,
                headers={"User-Agent": "KemonoDownloader/1.0"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if getattr(response, "status", 200) != 200:
                    return
                encoding = response.headers.get_content_charset() or "utf-8"
                text = response.read(128 * 1024).decode(encoding, errors="replace").strip()
        except Exception:
            return

        if text:
            self.loaded.emit(text)


class AnnouncementDialog(QDialog):
    def __init__(self, text: str, font_family: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("公告")
        self.setWindowIcon(load_app_icon())
        self.resize(560, 360)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title = QLabel("公告")
        title.setObjectName("announcementTitle")
        layout.addWidget(title)

        content = QPlainTextEdit()
        content.setObjectName("announcementContent")
        content.setReadOnly(True)
        content.setPlainText(text)
        layout.addWidget(content, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_button.clicked.connect(self.close)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self.setStyleSheet(
            f"""
            QDialog {{
                background: rgba(38, 38, 64, 245);
                color: rgba(255, 255, 255, 235);
                font-family: "{font_family}", "Microsoft YaHei UI", "Segoe UI";
                font-size: 14px;
            }}
            QLabel#announcementTitle {{
                color: rgba(255, 255, 255, 245);
                font-size: 18px;
                font-weight: 700;
            }}
            QPlainTextEdit#announcementContent {{
                background: rgba(255, 255, 255, 28);
                border: 1px solid rgba(255, 255, 255, 80);
                border-radius: 8px;
                color: rgba(255, 255, 255, 235);
                padding: 8px;
                outline: none;
            }}
            QPushButton {{
                min-height: 32px;
                border: 1px solid rgba(255, 255, 255, 150);
                border-radius: 8px;
                background: rgba(255, 255, 255, 180);
                color: #25313d;
                padding: 5px 18px;
                outline: none;
            }}
            QPushButton:focus {{
                outline: none;
            }}
            """
        )


class PostLoader(QObject):
    posts_loaded = pyqtSignal(list)
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            posts = asyncio.run(self._main())
            self.posts_loaded.emit(posts)
        except Exception as exc:
            self.error.emit(str(exc))

    async def _main(self) -> list[PostItem]:
        self.log.emit("正在读取帖子列表...")
        service = DownloadService(KemonoApi())
        posts = await service.list_posts(self.url)
        self.log.emit(f"读取完成，共 {len(posts)} 条帖子")
        return posts


class DownloadWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int, int)
    failed_file = pyqtSignal(str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        creator_url: str,
        posts: list[PostItem],
        output_dir: Path,
        concurrency: int,
    ):
        super().__init__()
        self.creator_url = creator_url
        self.posts = posts
        self.output_dir = output_dir
        self.concurrency = concurrency
        self.naming_mode = DateNamingMode.PREFIX
        self.failed_export_path = output_dir / FAILED_EXPORT_NAME
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.pause_event.set()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.error.emit(str(exc))

    async def _main(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.failed_export_path.exists():
            self.failed_export_path.unlink()

        service = DownloadService(KemonoApi())
        state = StateStore(f"kemono_{normalize_creator_path(self.creator_url)}")
        engine = DownloadEngine(
            concurrency=self.concurrency,
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        total_posts = len(self.posts)

        async def task_batches():
            async for batch in service.iter_post_task_batches(
                self.posts,
                self.output_dir,
                self.naming_mode,
            ):
                if batch:
                    post = batch[0].post
                    index = self.posts.index(post) + 1
                    self.log.emit(f"已整理 {index}/{total_posts}: {post.title}")
                yield batch

        def on_event(event: DownloadEvent) -> None:
            if event.type == "started":
                self.progress.emit(0, event.total or 0, 0)
                self.log.emit(f"开始流式处理 {len(self.posts)} 条帖子")
            elif event.type == "tasks_added":
                self.log.emit(event.message)
            elif event.type == "progress":
                self.progress.emit(event.done or 0, event.total or 0, event.failed or 0)
            elif event.type == "file_finished":
                self.log.emit(f"完成: {event.file_name}")
            elif event.type == "file_skipped":
                self.log.emit(f"跳过: {event.file_name}")
            elif event.type == "file_failed":
                text = self._format_failed_event(event)
                self._append_failed_export(text)
                self.failed_file.emit(f"{event.file_name} | {event.path}")
                self.log.emit(f"失败: {event.file_name} - {event.message}")
            elif event.type == "post_finished":
                if event.failed:
                    self.log.emit(f"帖子处理完成，有失败文件: {event.post_id}")
            elif event.type in {"finished", "cancelled"}:
                self.finished.emit(event.message)

        await engine.run_stream(task_batches(), state, on_event)

    def _format_failed_event(self, event: DownloadEvent) -> str:
        data = event.data or {}
        target_path = data.get("target_path") or str(event.path or "")
        target_dir = data.get("target_dir") or str(Path(target_path).parent if target_path else "")
        return "\n".join(
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

    def _append_failed_export(self, text: str) -> None:
        first_write = not self.failed_export_path.exists()
        with self.failed_export_path.open("a", encoding="utf-8") as file:
            if first_write:
                file.write("# Kemono Downloader failed files\n\n")
            file.write(text)


class MainWindow(QMainWindow):
    def __init__(self, font_family: str = DEFAULT_FONT_FAMILY):
        super().__init__()
        self.font_family = font_family
        self.posts: list[PostItem] = []
        self.loader: PostLoader | None = None
        self.worker: DownloadWorker | None = None
        self.announcement_loader: AnnouncementLoader | None = None
        self.announcement_dialog: AnnouncementDialog | None = None
        self.background_ratio = load_background_aspect_ratio()
        self._ratio_resize_guard = False
        self.setWindowTitle("Kemono Downloader")
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(self._ratio_size_for_width(820))
        self.resize(self._ratio_size_for_width(1120))

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        title = QLabel("Kemono Downloader")
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        layout.addWidget(self._build_top_panel())
        layout.addWidget(self._build_body(), stretch=1)
        layout.addWidget(self._build_bottom_panel())

        self._apply_style()
        self._set_running(False)

    def _build_top_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("topPanel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("overlayInput")
        self.url_input.setPlaceholderText("https://kemono.cr/fanbox/user/xxxx")
        self.path_input = QLineEdit()
        self.path_input.setObjectName("overlayInput")
        self.path_input.setPlaceholderText("选择保存目录")

        self.browse_button = QPushButton("浏览")
        self.load_button = QPushButton("加载帖子")
        self.load_button.setObjectName("primaryButton")
        self.browse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.load_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.browse_button.clicked.connect(self._browse)
        self.load_button.clicked.connect(self._load_posts)

        url_label = QLabel("创作者URL：")
        url_label.setObjectName("formLabel")
        path_label = QLabel("保存目录：")
        path_label.setObjectName("formLabel")

        grid.addWidget(url_label, 0, 0)
        grid.addWidget(self.url_input, 0, 1)
        grid.addWidget(self.load_button, 0, 2)
        grid.addWidget(path_label, 1, 0)
        grid.addWidget(self.path_input, 1, 1)
        grid.addWidget(self.browse_button, 1, 2)
        grid.setColumnStretch(1, 1)
        return panel

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("bodyPanel")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        self.summary_label = QLabel("等待加载帖子")
        self.summary_label.setObjectName("summaryLabel")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        body_layout.addWidget(self.summary_label)
        body_layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("primaryButton")
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.start_button.clicked.connect(self._start_download)
        self.pause_button.clicked.connect(self._pause)
        self.resume_button.clicked.connect(self._resume)
        self.stop_button.clicked.connect(self._stop)
        for button in [self.start_button, self.pause_button, self.resume_button, self.stop_button]:
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button)
        body_layout.addLayout(buttons)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("帖子列表")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.select_all_button = QPushButton("全选")
        self.invert_button = QPushButton("反选")
        self.select_all_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.invert_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.select_all_button.clicked.connect(self._select_all)
        self.invert_button.clicked.connect(self._invert_selection)
        title_row.addWidget(self.select_all_button)
        title_row.addWidget(self.invert_button)
        body_layout.addLayout(title_row)

        self.post_list = QListWidget()
        self.post_list.setObjectName("postList")
        self.post_list.setAlternatingRowColors(False)
        self.post_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.post_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        body_layout.addWidget(self.post_list, stretch=1)
        return body

    def _build_bottom_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.setObjectName("tabs")
        tabs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tabs.tabBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.failed_list = QListWidget()
        self.failed_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tabs.addTab(self.log_view, "日志")
        tabs.addTab(self.failed_list, "失败文件")
        tabs.setMinimumHeight(180)
        return tabs

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if path:
            self.path_input.setText(path)

    def _load_posts(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先填写创作者 URL")
            return

        self.post_list.clear()
        self.posts = []
        self.load_button.setEnabled(False)
        self.summary_label.setText("正在加载帖子...")
        self._log("开始加载帖子列表")
        self.loader = PostLoader(url)
        self.loader.posts_loaded.connect(self._on_posts_loaded)
        self.loader.error.connect(self._on_loader_error)
        self.loader.log.connect(self._log)
        self.loader.start()

    def _on_posts_loaded(self, posts: list[PostItem]) -> None:
        self.posts = posts
        self.post_list.clear()
        for post in posts:
            item = QListWidgetItem(f"{post.day}  {post.title}")
            item.setData(Qt.ItemDataRole.UserRole, post.id)
            self.post_list.addItem(item)
            item.setSelected(True)
        self.summary_label.setText(f"已加载 {len(posts)} 条帖子，默认全选")
        self.load_button.setEnabled(True)

    def _on_loader_error(self, message: str) -> None:
        self.load_button.setEnabled(True)
        self.summary_label.setText("加载失败")
        self._log(f"加载失败: {message}")
        QMessageBox.warning(self, "加载失败", message)

    def _start_download(self) -> None:
        selected = self._selected_posts()
        output = self.path_input.text().strip()
        url = self.url_input.text().strip()
        if not selected:
            QMessageBox.warning(self, "提示", "请至少选择一条帖子")
            return
        if not output:
            QMessageBox.warning(self, "提示", "请选择保存目录")
            return
        self.failed_list.clear()
        self.progress.setValue(0)
        self.progress.setMaximum(0)
        self.summary_label.setText("正在准备下载任务...")
        self._set_running(True)

        self.worker = DownloadWorker(
            creator_url=url,
            posts=selected,
            output_dir=Path(output),
            concurrency=DEFAULT_CONCURRENCY,
        )
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._on_progress)
        self.worker.failed_file.connect(self._on_failed_file)
        self.worker.finished.connect(self._on_download_finished)
        self.worker.error.connect(self._on_download_error)
        self.worker.start()

    def _selected_posts(self) -> list[PostItem]:
        selected_ids: set[str] = set()
        for index in range(self.post_list.count()):
            item = self.post_list.item(index)
            if item.isSelected():
                selected_ids.add(str(item.data(Qt.ItemDataRole.UserRole)))
        return [post for post in self.posts if post.id in selected_ids]

    def _select_all(self) -> None:
        for index in range(self.post_list.count()):
            self.post_list.item(index).setSelected(True)

    def _invert_selection(self) -> None:
        for index in range(self.post_list.count()):
            item = self.post_list.item(index)
            item.setSelected(not item.isSelected())

    def _pause(self) -> None:
        if self.worker:
            self.worker.pause()
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(True)
            self._log("已暂停")

    def _resume(self) -> None:
        if self.worker:
            self.worker.resume()
            self.pause_button.setEnabled(True)
            self.resume_button.setEnabled(False)
            self._log("已继续")

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self._log("正在停止...")

    def _on_progress(self, done: int, total: int, failed: int) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.summary_label.setText(f"文件进度 {done}/{total}，失败 {failed}")

    def _on_failed_file(self, text: str) -> None:
        self.failed_list.addItem(text)

    def _on_download_finished(self, message: str) -> None:
        self._log(message)
        if self.failed_list.count():
            failed_path = Path(self.path_input.text().strip()) / FAILED_EXPORT_NAME
            self._log(f"失败记录已导出: {failed_path}")
        self.summary_label.setText(message)
        self._set_running(False)

    def _on_download_error(self, message: str) -> None:
        self._log(f"下载出错: {message}")
        self.summary_label.setText("下载出错")
        self._set_running(False)
        QMessageBox.warning(self, "下载出错", message)

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.load_button.setEnabled(not running)
        self.pause_button.setEnabled(running)
        self.resume_button.setEnabled(False)
        self.stop_button.setEnabled(running)

    def _log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def start_announcement_loader(self) -> None:
        self.announcement_loader = AnnouncementLoader()
        self.announcement_loader.loaded.connect(self._show_announcement)
        self.announcement_loader.start()

    def _show_announcement(self, text: str) -> None:
        dialog = AnnouncementDialog(text, self.font_family)
        dialog.finished.connect(lambda _result: setattr(self, "announcement_dialog", None))
        self.announcement_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def closeEvent(self, event) -> None:
        if self.worker:
            self.worker.stop()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._ratio_resize_guard:
            return

        size = event.size()
        if size.height() <= 0:
            return
        if abs((size.width() / size.height()) - self.background_ratio) < 0.01:
            return

        old_size = event.oldSize()
        width_changed = old_size.isValid() and abs(size.width() - old_size.width()) >= abs(
            size.height() - old_size.height()
        )
        next_size = (
            self._ratio_size_for_width(size.width())
            if width_changed or not old_size.isValid()
            else self._ratio_size_for_height(size.height())
        )

        self._ratio_resize_guard = True
        self.resize(next_size)
        self._ratio_resize_guard = False

    def _ratio_size_for_width(self, width: int) -> QSize:
        return QSize(width, max(1, round(width / self.background_ratio)))

    def _ratio_size_for_height(self, height: int) -> QSize:
        return QSize(max(1, round(height * self.background_ratio)), height)

    def _apply_style(self) -> None:
        background_rule = "background: #000000;"
        if BACKGROUND_PATH.exists():
            background_rule = (
                f'border-image: url("{BACKGROUND_PATH.as_posix()}") '
                "0 0 0 0 stretch stretch;"
            )
        style = """
            QWidget#root {
                __BACKGROUND_RULE__
                color: rgba(255, 255, 255, 235);
                font-family: "__FONT_FAMILY__", "Microsoft YaHei UI", "Segoe UI";
                font-size: 14px;
            }
            QWidget#topPanel, QWidget#bodyPanel {
                background: transparent;
                border: none;
            }
            QLabel#titleLabel {
                font-size: 28px;
                font-weight: 700;
                color: rgba(255, 255, 255, 235);
                padding: 2px 0 4px 0;
            }
            QLabel#formLabel, QLabel#sectionTitle {
                color: rgba(255, 255, 255, 245);
                font-weight: 700;
            }
            QLabel#sectionTitle {
                font-size: 16px;
                padding: 6px 0 0 0;
            }
            QLabel#summaryLabel {
                color: rgba(255, 255, 255, 225);
                padding: 4px 0 0 0;
            }
            QLineEdit {
                min-height: 34px;
                border: 1px solid rgba(255, 255, 255, 92);
                border-radius: 8px;
                background: rgba(38, 38, 64, 120);
                color: rgba(255, 255, 255, 240);
                padding: 4px 10px;
                selection-background-color: #3478f6;
            }
            QLineEdit:focus {
                border: 1px solid rgba(255, 255, 255, 170);
                background: rgba(38, 38, 64, 150);
            }
            QLineEdit::placeholder {
                color: rgba(255, 255, 255, 145);
            }
            QPushButton, QListWidget, QListWidget::item, QTabBar::tab {
                outline: none;
            }
            QPushButton {
                min-height: 34px;
                border: 1px solid rgba(255, 255, 255, 150);
                border-radius: 8px;
                background: rgba(255, 255, 255, 180);
                color: #25313d;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 225);
                border-color: rgba(92, 150, 255, 190);
            }
            QPushButton:pressed {
                background: rgba(219, 234, 254, 220);
            }
            QPushButton:focus {
                outline: none;
                border: 1px solid rgba(255, 255, 255, 150);
            }
            QPushButton:disabled {
                color: rgba(95, 111, 127, 140);
                background: rgba(236, 240, 244, 135);
                border-color: rgba(255, 255, 255, 110);
            }
            QPushButton#primaryButton {
                background: rgba(52, 120, 246, 220);
                border-color: rgba(52, 120, 246, 235);
                color: white;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover {
                background: rgba(37, 107, 232, 235);
            }
            QPushButton#dangerButton {
                background: rgba(255, 245, 245, 190);
                border-color: rgba(242, 184, 184, 190);
                color: #b42318;
            }
            QPushButton#dangerButton:hover {
                background: rgba(255, 231, 231, 230);
            }
            QListWidget, QPlainTextEdit {
                background: rgba(38, 38, 64, 120);
                border: 1px solid rgba(255, 255, 255, 92);
                border-radius: 8px;
                color: rgba(255, 255, 255, 232);
                padding: 8px;
            }
            QListWidget::item {
                min-height: 30px;
                border-radius: 6px;
                padding: 4px 6px;
                color: rgba(255, 255, 255, 232);
            }
            QListWidget::item:hover {
                background: rgba(255, 255, 255, 42);
            }
            QListWidget::item:selected {
                background: rgba(255, 255, 255, 70);
                color: rgba(255, 255, 255, 245);
            }
            QListWidget::item:focus {
                outline: none;
            }
            QProgressBar {
                min-height: 18px;
                border: 1px solid rgba(255, 255, 255, 92);
                border-radius: 9px;
                background: rgba(38, 38, 64, 120);
                color: rgba(255, 255, 255, 235);
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 8px;
                background: rgba(52, 120, 246, 215);
            }
            QTabWidget#tabs {
                background: transparent;
                border: none;
            }
            QTabWidget::pane {
                border: none;
                background: transparent;
            }
            QTabBar::tab {
                min-height: 30px;
                padding: 6px 16px;
                border: 1px solid rgba(255, 255, 255, 80);
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                background: rgba(38, 38, 64, 120);
                color: rgba(255, 255, 255, 215);
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background: rgba(38, 38, 64, 170);
                color: rgba(255, 255, 255, 245);
                font-weight: 600;
            }
            """
        self.setStyleSheet(
            style.replace("__BACKGROUND_RULE__", background_rule).replace(
                "__FONT_FAMILY__",
                self.font_family,
            )
        )


def main() -> int:
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    font_family = load_app_font()
    app.setFont(QFont(font_family, 10))
    app.setWindowIcon(load_app_icon())
    window = MainWindow(font_family)
    window.show()
    window.start_announcement_loader()
    return app.exec()


def load_app_font() -> str:
    if not FONT_PATH.exists():
        return DEFAULT_FONT_FAMILY

    font_id = QFontDatabase.addApplicationFont(str(FONT_PATH))
    if font_id < 0:
        return DEFAULT_FONT_FAMILY

    families = QFontDatabase.applicationFontFamilies(font_id)
    return families[0] if families else DEFAULT_FONT_FAMILY


def load_app_icon() -> QIcon:
    if not ICON_PATH.exists():
        return QIcon()
    return QIcon(str(ICON_PATH))


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        return


def load_background_aspect_ratio() -> float:
    pixmap = QPixmap(str(BACKGROUND_PATH))
    if pixmap.isNull() or pixmap.height() <= 0:
        return 16 / 9
    return pixmap.width() / pixmap.height()


if __name__ == "__main__":
    raise SystemExit(main())
