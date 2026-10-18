"""下載紀錄與收集進度。Read-only native history and resumable-source views."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
import re

from PyQt6.QtCore import QDateTime, QItemSelectionModel, QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

import storage
from i18n import tr, translate_text, locale as ui_locale
from resources_rc import get_icon_path
from redownload_dialog import choose_redownload_mode
from author_avatar import AvatarLoader, avatar_pixmap, valid_avatar_url
from media_preview import MediaPreviewLoader, preview_signature
from ui_design import PreviewLabel, line_icon
from ui_theme import apply_dialog_theme, apply_palette, theme_colors


def _tip(value):
    return "<qt>" + escape(str(value)[:4000]).replace("\n", "<br>") + "</qt>"


def _label(text="", muted=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if muted:
        label.setProperty("role", "muted")
    return label


def _table(columns):
    table = QTableWidget(0, columns)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setWordWrap(False)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(34)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.setMinimumHeight(160)
    return table


def _cell(value):
    text = str(value or "")[:4000]
    item = QTableWidgetItem(text)
    item.setToolTip(_tip(text))
    return item


def _scroll_page(content):
    # 小視窗或長翻譯可捲動整個分頁，不讓表格與說明強迫視窗長高或互相重疊。
    # Scroll tab contents on short windows instead of expanding or overlapping child controls.
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


def _date(value):
    try:
        timestamp = datetime.fromisoformat(str(value)).astimezone().timestamp()
        return ui_locale().toString(QDateTime.fromMSecsSinceEpoch(int(timestamp * 1000)), "yyyy-MM-dd HH:mm")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value or "")[:32]


class _QuerySignals(QObject):
    finished = pyqtSignal(int, str, object, str)


class _HistoryQuery(QRunnable):
    def __init__(self, generation, kind, db_path, arguments):
        super().__init__()
        self.generation, self.kind = generation, kind
        self.db_path, self.arguments = db_path, arguments
        self.signals = _QuerySignals()

    def run(self):
        # 每次查詢各開唯讀連線，不把下載執行緒的 Store 跨執行緒共享。
        # Open a separate read-only connection; never share the downloader's Store across threads.
        result, error = None, ""
        try:
            reader = {"history":storage.fetch_history,"jobs":storage.fetch_jobs,"sources":storage.fetch_job_sources}[self.kind]
            result = reader(self.db_path, **self.arguments)
        except storage.HistoryReadError as failure:
            error = str(failure)
        except Exception:
            error = "無法讀取下載紀錄，請稍後重新整理。"
        try:
            self.signals.finished.emit(self.generation, self.kind, result, error)
        except RuntimeError:
            pass  # 視窗可能已被釋放。The dialog may already have been destroyed.


class HistoryPanel(QWidget):
    retryRequested = pyqtSignal(object)
    redownloadRequested = pyqtSignal(object, str)

    def __init__(self, parent=None, db_path=None, theme="light", *, embedded=True, session_log=None):
        super().__init__(parent)
        if db_path is None:
            raise ValueError("HistoryDialog requires a database path")
        self.db_path = Path(db_path)
        self._embedded = embedded
        self.theme_mode = theme
        self.setProperty("i18nSkip", True)
        self.setWindowIcon(QIcon(get_icon_path()))
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._active = self._pending = None
        self._loading = False
        self._retain_preview = False
        self._hidden = False
        self._retry_allowed = True
        self._page, self._total = 1, 0
        self._rows, self._jobs, self._sources = [], [], []
        self._last_error = ""
        self._session_entries = []
        self._preview_key = self._preview_data = self._preview_kind = None
        self._preview_image = self._avatar_image = self._preview_signature = None
        self._avatar_route = (False, 0)
        self._avatar_loader = AvatarLoader(self)
        self._preview_loader = MediaPreviewLoader(self)
        self._avatar_loader.ready.connect(self._avatar_ready)
        self._preview_loader.ready.connect(self._preview_ready)
        # 父視窗直接釋放也要取消；背景工作只連到 QObject slot，不保留視窗閉包。
        # Direct parent destruction cancels work too; background signals use QObject slots, not window closures.
        self.destroyed.connect(self._avatar_loader.close)
        self.destroyed.connect(self._preview_loader.close)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._new_search)
        self._live_refresh = QTimer(self)
        self._live_refresh.setSingleShot(True)
        self._live_refresh.setInterval(1000)
        self._live_refresh.timeout.connect(lambda: self.refresh(preserve_preview=True))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(0, 0, 0, 0) if embedded else (24, 22, 24, 20))
        layout.setSpacing(10 if embedded else 14)
        self.title_label = _label()
        self.title_label.setProperty("role", "dialogTitle")
        layout.addWidget(self.title_label)
        self.title_label.setVisible(not embedded)
        self.tabs = None if embedded else QTabWidget()
        if self.tabs is not None:
            layout.addWidget(self.tabs, 1)

        # 紀錄頁只載入目前一頁；本機檔案與來源都必須明確點按才開啟。
        # Load one history page at a time; files and source posts open only on explicit activation.
        history = QWidget()
        body = QVBoxLayout(history)
        body.setContentsMargins(0, 14, 0, 0)
        body.setSpacing(10)
        filters = QHBoxLayout()
        self.status_label = _label()
        self.status_combo = QComboBox()
        for value in ("all", "downloaded", "failed"):
            self.status_combo.addItem("", value)
        self.status_label.setBuddy(self.status_combo)
        self.search_label = _label()
        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaxLength(200)
        self.search_label.setBuddy(self.search_input)
        self.refresh_button = QPushButton()
        for widget in (self.status_label, self.status_combo, self.search_label):
            filters.addWidget(widget)
        filters.addWidget(self.search_input, 1)
        filters.addWidget(self.refresh_button)
        body.addLayout(filters)
        self.history_table = _table(4)
        self.history_table.setMinimumHeight(140)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        body.addWidget(self.history_table, 1)
        # 沿用圖庫的頭像與圖片元件；單筆已完成紀錄才啟用小型本機預覽。
        # Reuse gallery avatar/image components; enable a compact local preview for one completed record.
        self.preview_panel = QWidget()
        preview_layout = QHBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(0, 2, 0, 2)
        preview_layout.setSpacing(16)
        identity = QVBoxLayout()
        identity.setSpacing(5)
        self.preview_author_row = QWidget()
        author_layout = QHBoxLayout(self.preview_author_row)
        author_layout.setContentsMargins(0, 0, 0, 0)
        author_layout.setSpacing(10)
        self.preview_avatar_label = _label()
        self.preview_avatar_label.setFixedSize(40, 40)
        author_layout.addWidget(self.preview_avatar_label)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.preview_author_label = _label()
        self.preview_author_label.setProperty("role", "field")
        self.preview_handle_label = _label(muted=True)
        names.addWidget(self.preview_author_label)
        names.addWidget(self.preview_handle_label)
        author_layout.addLayout(names, 1)
        identity.addWidget(self.preview_author_row)
        self.preview_status_label = _label(muted=True)
        identity.addWidget(self.preview_status_label)
        preview_layout.addLayout(identity, 1)
        self.preview_image_label = PreviewLabel()
        self.preview_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image_label.setFixedSize(232, 132)
        preview_layout.addWidget(self.preview_image_label)
        body.addWidget(self.preview_panel)
        self.detail_label = _label(muted=True)
        self.detail_label.setMaximumHeight(88)
        body.addWidget(self.detail_label)
        actions = QHBoxLayout()
        self.file_button, self.folder_button, self.post_button = (QPushButton() for _ in range(3))
        for button in (self.file_button, self.folder_button, self.post_button):
            actions.addWidget(button)
        actions.addStretch(1)
        body.addLayout(actions)
        pager = QHBoxLayout()
        self.previous_button, self.next_button = QPushButton(), QPushButton()
        self.page_label = _label(muted=True)
        pager.addWidget(self.previous_button)
        pager.addWidget(self.next_button)
        pager.addStretch(1)
        pager.addWidget(self.page_label)
        body.addLayout(pager)
        self.records_scroll = _scroll_page(history)
        if self.tabs is not None:
            self.tabs.addTab(self.records_scroll, "")

        # 工作頁顯示實際來源狀態，不把搜尋跑完等同於已收齊 X 的所有作品。
        # Show recorded source states without equating completed searches with exhaustive X coverage.
        progress = QWidget()
        progress_layout = QVBoxLayout(progress)
        progress_layout.setContentsMargins(0, 14, 0, 0)
        progress_layout.setSpacing(10)
        # 本次完整日誌與持久化來源進度共用第二分頁，主頁只保留簡要回饋。
        # Keep the full session log and persisted source progress in the second tab; the main page stays compact.
        self.session_log_hint = _label(muted=True)
        progress_layout.addWidget(self.session_log_hint)
        self.session_log = session_log if session_log is not None else QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setMinimumHeight(170)
        self.session_log.setMaximumHeight(260)
        self.session_log.document().setMaximumBlockCount(2000)
        progress_layout.addWidget(self.session_log)
        self.jobs_hint = _label(muted=True)
        progress_layout.addWidget(self.jobs_hint)
        self.jobs_table = _table(4)
        self.jobs_table.setMinimumHeight(110)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_table.setMaximumHeight(190)
        self.jobs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        progress_layout.addWidget(self.jobs_table)
        self.source_hint = _label(muted=True)
        progress_layout.addWidget(self.source_hint)
        self.sources_table = _table(5)
        self.sources_table.setMinimumHeight(140)
        self.sources_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sources_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sources_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        progress_layout.addWidget(self.sources_table, 1)
        if self.tabs is not None:
            self.tabs.addTab(_scroll_page(progress), "")
        else:
            # 主視窗的狀況頁直接並排日誌與作品，不再套入另一組分頁。
            # Embed the log and records side by side in the main activity page, without nested tabs.
            self.activity_splitter = QSplitter(Qt.Orientation.Horizontal)
            log_column = QWidget()
            log_layout = QVBoxLayout(log_column)
            log_layout.setContentsMargins(0, 10, 0, 0)
            log_layout.setSpacing(8)
            progress_layout.removeWidget(self.session_log_hint)
            progress_layout.removeWidget(self.session_log)
            self.session_log.setMaximumHeight(16777215)
            self.session_log.setMinimumWidth(260)
            log_layout.addWidget(self.session_log_hint)
            log_layout.addWidget(self.session_log, 1)
            self.progress_toggle = QToolButton()
            self.progress_toggle.setCheckable(True)
            self.progress_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.progress_toggle.setArrowType(Qt.ArrowType.RightArrow)
            self.progress_toggle.clicked.connect(self._toggle_progress)
            log_layout.addWidget(self.progress_toggle)
            self.progress_scroll = _scroll_page(progress)
            self.progress_scroll.setMaximumHeight(270)
            self.progress_scroll.hide()
            log_layout.addWidget(self.progress_scroll)
            self.activity_splitter.addWidget(log_column)
            self.activity_splitter.addWidget(self.records_scroll)
            self.activity_splitter.setStretchFactor(0, 2)
            self.activity_splitter.setStretchFactor(1, 3)
            self.activity_splitter.setSizes([400, 600])
            layout.addWidget(self.activity_splitter, 1)
        self.message_label = _label(muted=True)
        layout.addWidget(self.message_label)
        # 長翻譯不擠壓三個並排按鈕；左側動作分列，關閉固定在右下角。
        # Stack the two longer actions and keep Close at the right in every language.
        footer = QGridLayout()
        footer.setVerticalSpacing(8)
        self.retry_button, self.redownload_button, self.close_button = QPushButton(), QPushButton(), QPushButton()
        self.retry_button.setProperty("variant", "primary")
        footer.addWidget(self.retry_button, 0, 0)
        footer.addWidget(self.redownload_button, 0 if embedded else 1, 1 if embedded else 0)
        footer.setColumnStretch(2 if embedded else 1, 1)
        footer.addWidget(self.close_button, 1, 2)
        self.close_button.setVisible(not embedded)
        layout.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.status_combo.currentIndexChanged.connect(self._new_search)
        self.search_input.textChanged.connect(lambda:self._debounce.start())
        self.search_input.returnPressed.connect(self._new_search)
        self.refresh_button.clicked.connect(self.refresh)
        self.previous_button.clicked.connect(lambda:self._change_page(-1))
        self.next_button.clicked.connect(lambda:self._change_page(1))
        self.history_table.itemSelectionChanged.connect(self._selection_changed)
        self.jobs_table.itemSelectionChanged.connect(self._job_selected)
        if self.tabs is not None:
            self.tabs.currentChanged.connect(self.refresh)
        self.retry_button.clicked.connect(self._retry_selected)
        self.redownload_button.clicked.connect(self._redownload_selected)
        self.file_button.clicked.connect(lambda:self._open_local("file"))
        self.folder_button.clicked.connect(lambda:self._open_local("folder"))
        self.post_button.clicked.connect(self._open_post)
        self.close_button.clicked.connect(self.close)
        self.set_theme(theme)
        self.refresh_language()

    def _section(self):
        return self.tabs.currentIndex() if self.tabs is not None else 0

    def _toggle_progress(self, checked):
        self.progress_scroll.setVisible(checked)
        self.progress_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        if checked:
            self._request("jobs", limit=20, preserve_preview=True)

    def schedule_refresh(self):
        # 下載事件最多每秒合併一次唯讀查詢；隱藏時留待下次顯示，不搶走設定頁。
        # Coalesce download events to at most one read query per second; hidden pages refresh when shown.
        if self.isVisible() and not self._hidden and not self._live_refresh.isActive():
            self._live_refresh.start()

    @staticmethod
    def _render_session_entry(template, values):
        if values is None:
            return translate_text(template)
        arguments = {key: translate_text(str(value)) if key in {"exc", "message", "state", "operation"} else value
                     for key, value in values.items()}
        return tr(template, **arguments)

    def set_session_log(self, entries):
        # 重開時以主控制器的有限快照對齊，不重複加入隱藏期間已接收的訊息。
        # Resync from the controller's bounded snapshot on reopen without duplicating hidden-window updates.
        self._session_entries = [(str(template), dict(values) if values is not None else None)
                                 for template, values in entries[-2000:]]
        self._render_session_log()

    def append_session_log(self, entry):
        template, values = entry
        entry = (str(template), dict(values) if values is not None else None)
        self._session_entries.append(entry)
        del self._session_entries[:-2000]
        # appendPlainText 保持外部檔名與訊息為純文字，不把它們當成 HTML。
        # appendPlainText keeps external filenames/messages literal instead of interpreting HTML.
        self.session_log.appendPlainText(self._render_session_entry(*entry))

    def _render_session_log(self):
        self.session_log.setPlainText("\n".join(self._render_session_entry(*entry) for entry in self._session_entries))
        self.session_log.moveCursor(QTextCursor.MoveOperation.End)

    def _request(self, kind, *, preserve_preview=False, **arguments):
        self._generation += 1
        self._loading, self._last_error = True, ""
        self._retain_preview = preserve_preview
        request = (self._generation, kind, arguments)
        # 同時最多一筆查詢與一筆最新待查條件，快速打字不會堆積無限工作。
        # Keep one active query and only the newest pending request during rapid input.
        if self._active is not None:
            self._pending = request
        else:
            self._start_query(request)
        self._update_controls()

    def _start_query(self, request):
        generation, kind, arguments = request
        task = _HistoryQuery(generation, kind, self.db_path, arguments)
        self._active = task
        task.signals.finished.connect(self._query_finished)
        self._pool.start(task)

    @pyqtSlot(int, str, object, str)
    def _query_finished(self, generation, kind, result, error):
        # 只接受最新查詢；關窗或切換條件後，舊結果不能改寫目前畫面。
        # Accept only the newest query so closed dialogs and changed filters reject stale results.
        self._active = None
        pending, self._pending = self._pending, None
        if not self._hidden and generation == self._generation:
            self._loading, self._last_error = False, error
            if not error:
                if kind == "history":
                    selected = {row["media_key"] for row in self._selected_media()}
                    self._rows, self._page, self._total = result["items"], result["page"], result["total"]
                    self._render_history(selected)
                elif kind == "jobs":
                    indices = self.jobs_table.selectionModel().selectedRows()
                    selected_job = self._jobs[indices[0].row()]["job_id"] if indices and indices[0].row() < len(self._jobs) else None
                    self._jobs = result
                    self._sources = []
                    self._render_jobs()
                    self._render_sources()
                    if self._jobs:
                        # 同一列可能仍被選取；先清除，確保重新整理也會讀取來源。
                        # Clear a retained row so refreshing the same job reloads its sources.
                        self.jobs_table.blockSignals(True)
                        self.jobs_table.clearSelection()
                        self.jobs_table.blockSignals(False)
                        index = next((index for index, job in enumerate(self._jobs) if job["job_id"] == selected_job), 0)
                        self.jobs_table.selectRow(index)
                else:
                    self._sources = result
                    self._render_sources()
            self._update_controls()
            # 展開的工作進度跟隨紀錄刷新，依序查工作與所選來源，不另排無限工作。
            # Refresh expanded progress after records, chaining jobs then selected sources within the same bounded queue.
            if not error and kind == "history" and self._embedded and self.progress_toggle.isChecked():
                self._request("jobs", limit=20, preserve_preview=True)
        if not self._hidden and pending is not None and pending[0] == self._generation:
            self._start_query(pending)

    def refresh(self, *_, preserve_preview=False):
        if self._section() == 0:
            self._request("history", status=self.status_combo.currentData(), search=self.search_input.text(), page=self._page, page_size=40,
                          preserve_preview=preserve_preview)
        else:
            self._request("jobs", limit=20)

    def _new_search(self, *_):
        self._debounce.stop()
        self._page = 1
        if self._section() == 0:
            self.refresh()

    def _change_page(self, delta):
        self._page = max(1, self._page + delta)
        self.refresh()

    def _selected_media(self):
        return [self._rows[index.row()] for index in self.history_table.selectionModel().selectedRows()
                if index.row() < len(self._rows)]

    def _selection_changed(self):
        self._update_controls()

    def configure_route(self, warp_proxy: bool, port: int):
        # 紀錄頭像遵循主視窗的路由；切換時取消舊請求，WARP 不降級為直連。
        # History avatars follow the main window route; changing it cancels requests without direct fallback.
        if warp_proxy and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("Invalid local proxy port")
        route = (bool(warp_proxy), port if warp_proxy else 0)
        if route == self._avatar_route:
            return
        self._cancel_previews()
        self._avatar_loader.configure(warp_proxy, port)
        self._avatar_route = route
        self._update_controls()

    def _cancel_previews(self):
        self._avatar_loader.cancel()
        self._preview_loader.cancel()
        self._preview_key = self._preview_data = self._preview_kind = None
        self._preview_image = self._avatar_image = self._preview_signature = None

    def _sync_preview(self, single):
        # 選取、篩選、分頁及顯示狀態共用同一個身分，不能讓舊結果回填新列。
        # Selection, filters, pages and visibility share one identity so stale results cannot refill a new row.
        eligible = (single is not None and single.get("state") == "downloaded"
                    and not self._hidden and self.isVisible() and (not self._loading or self._retain_preview)
                    and self._section() == 0)
        key = tuple(single.get(field) for field in ("media_key", "author", "author_name", "avatar_url", "media_type",
                    "safe_file_path", "safe_folder_path", "preview_allowed")) if eligible else None
        if key == self._preview_key:
            self._refresh_preview_widgets()
            return
        self._cancel_previews()
        if eligible:
            self._preview_key = key
            handle = str(single.get("author") or "")
            self._preview_data = {"author_name": str(single.get("author_name") or "")[:100],
                                  "author": handle if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) else ""}
            self._preview_kind = "unavailable"
            avatar = single.get("avatar_url")
            if valid_avatar_url(avatar):
                self._avatar_loader.load(avatar)
            path, root = single.get("safe_file_path"), single.get("safe_folder_path")
            # safe 路徑仍是快照；讀取前再次排除被換成 reparse point 的目錄與檔案。
            # Safe paths are snapshots; recheck replaced reparse directories/files before a read.
            allowed = (single.get("preview_allowed") and path and root
                       and storage._history_preview_allowed({"path": path, "target_dir": root}))
            self._preview_signature = preview_signature(path, root) if allowed else None
            if allowed:
                if single.get("media_type") == "video":
                    self._preview_kind = "video"
                elif single.get("media_type") == "image" and self._preview_signature is not None:
                    self._preview_kind = "loading"
                    self._preview_loader.load(path, root)
        self._refresh_preview_widgets()
        if eligible:
            QTimer.singleShot(0, self._reveal_preview)

    @pyqtSlot()
    def _reveal_preview(self):
        # 小視窗選取後捲到預覽與開檔動作；不讓新增圖片藏在分頁下緣。
        # Reveal the preview and Open actions after selection on short windows.
        if self._preview_is_current():
            self.records_scroll.ensureWidgetVisible(self.file_button, 0, 4)

    def _preview_is_current(self):
        selected = self._selected_media()
        return (not self._hidden and self.isVisible() and (not self._loading or self._retain_preview) and self._section() == 0
                and self._preview_key is not None and len(selected) == 1
                and selected[0].get("state") == "downloaded" and selected[0].get("media_key") == self._preview_key[0])

    @pyqtSlot(object)
    def _avatar_ready(self, image):
        if self._preview_is_current():
            self._avatar_image = image
            self._refresh_preview_widgets()

    @pyqtSlot(object)
    def _preview_ready(self, image):
        if self._preview_is_current() and self._preview_kind == "loading":
            signature = self._preview_signature
            valid = signature is not None and preview_signature(signature[0], signature[1]) == signature
            self._preview_image = image if valid else None
            self._preview_kind = "image" if self._preview_image is not None else "unavailable"
            self._refresh_preview_widgets()

    def _refresh_preview_widgets(self):
        if not hasattr(self, "preview_panel"):
            return
        colors = theme_colors(self.theme_mode)
        self.preview_author_row.setVisible(self._preview_data is not None)
        self.preview_image_label.setVisible(self._preview_data is not None)
        data = self._preview_data or {}
        name, handle = data.get("author_name", ""), data.get("author", "")
        self.preview_author_label.setText(name or ("@" + handle if handle else tr("作者資料未提供")))
        self.preview_author_label.setToolTip(_tip(name))
        self.preview_handle_label.setText("@" + handle if handle else "")
        self.preview_handle_label.setVisible(bool(handle and name))
        self.preview_avatar_label.setPixmap(avatar_pixmap(self._avatar_image, colors))
        self.preview_avatar_label.setAccessibleName(tr("作者"))
        self.preview_avatar_label.setToolTip(tr("作者公開頭像") if self._avatar_image is not None
                                            else tr("未提供頭像，或頭像暫時無法載入。"))
        captions = {None: "選取一筆已完成的紀錄以預覽本機圖片。", "loading": "正在載入本機預覽…",
                    "unavailable": "本機預覽無法使用；請開啟檔案確認。", "video": "影片不會自動播放；請使用「開啟檔案」。",
                    "image": "本機圖片預覽"}
        self.preview_status_label.setText(tr(captions[self._preview_kind]))
        self.preview_image_label.setAccessibleName(tr("本機圖片預覽"))
        self.preview_image_label.setStyleSheet(
            f"background:{colors['log']};border:1px solid {colors['border']};border-radius:7px;")
        self.preview_image_label.set_preview(self._preview_image,
            line_icon("video" if self._preview_kind == "video" else "image", 42, colors["muted"]))

    def _job_selected(self):
        indices = self.jobs_table.selectionModel().selectedRows()
        if indices and indices[0].row() < len(self._jobs):
            self._sources = []
            self._render_sources()
            self._request("sources", job_id=self._jobs[indices[0].row()]["job_id"], limit=200,
                          preserve_preview=self._embedded)

    @staticmethod
    def _state(value):
        return tr({"downloaded":"已完成","failed":"失敗","pending":"尚未完成","queued":"等待中",
                   "running":"進行中","completed":"已完成","partial":"部分完成","stopped":"已停止"}.get(value, "尚未完成"))

    def _render_history(self, selected=None):
        if selected is None:
            selected = {row["media_key"] for row in self._selected_media()}
        self.history_table.blockSignals(True)
        self.history_table.clearSelection()
        self.history_table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            values = (self._state(row["state"]), "@"+row["author"] if row["author"] else tr("作者資料未提供"), row["filename"], _date(row["downloaded_at"] or row["updated_at"]))
            for column, value in enumerate(values):
                self.history_table.setItem(index, column, _cell(value))
            if row["media_key"] in selected:
                self.history_table.selectionModel().select(self.history_table.model().index(index, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.history_table.blockSignals(False)

    def _render_jobs(self):
        self.jobs_table.blockSignals(True)
        self.jobs_table.setRowCount(len(self._jobs))
        for index, job in enumerate(self._jobs):
            options = job.get("options", {})
            tags = options.get("hashtags", options.get("tags", options.get("hashtag", "")))
            summary = " ".join("#"+str(tag).lstrip("#") for tag in tags) if isinstance(tags, (tuple,list)) else str(tags)
            values = (self._state(job["state"]), summary, tr("已完成 {done} / {total} 個來源", done=job["source_completed"],total=job["source_total"]), _date(job["updated_at"]))
            for column, value in enumerate(values):
                self.jobs_table.setItem(index, column, _cell(value))
        self.jobs_table.blockSignals(False)

    def _render_sources(self):
        self.sources_table.setRowCount(len(self._sources))
        kinds = {"search":"標籤搜尋","author":"作者媒體補抓","direct":"貼文網址","retry":"重試未完成","thread":"同串原作者留言"}
        for index, source in enumerate(self._sources):
            progress = source.get("progress", {})
            count = progress.get("records")
            detail = tr("已讀取 {count} 筆",count=count) if type(count) is int and count >= 0 else tr("尚無進度")
            if progress.get("quota_exhausted"):
                detail = tr("已達搜尋上限")
            values = (tr(kinds.get(source["kind"], "收集來源")), translate_text(source["name"]), self._state(source["state"]), detail, translate_text(source.get("message", "")))
            for column, value in enumerate(values):
                self.sources_table.setItem(index, column, _cell(value))

    def _update_controls(self):
        selected = self._selected_media()
        single = selected[0] if len(selected) == 1 else None
        self._sync_preview(single)
        self.retry_button.setVisible(self._section() == 0)
        self.redownload_button.setVisible(self._section() == 0)
        self.retry_button.setEnabled(self._retry_allowed and not self._loading and any(row["state"] == "failed" for row in selected))
        self.redownload_button.setEnabled(self._retry_allowed and not self._loading and any(row["state"] == "downloaded" for row in selected))
        self.file_button.setEnabled(bool(single and single.get("safe_file_path")))
        self.folder_button.setEnabled(bool(single and single.get("safe_folder_path")))
        self.post_button.setEnabled(bool(single and re.fullmatch(r"[0-9]{1,30}", str(single["post_id"]))))
        self.previous_button.setEnabled(not self._loading and self._page > 1)
        self.next_button.setEnabled(not self._loading and self._page * 40 < self._total)
        self.page_label.setText(tr("第 {page} / {pages} 頁 · 共 {total} 個媒體",page=self._page,pages=max(1,(self._total+39)//40),total=self._total))
        if single:
            # 畫面只顯示簡短檔名；完整路徑留在提示中，避免壓縮預覽與動作的空間。
            # Keep the visible filename short; retain the full path in the tooltip to protect preview/action space.
            detail = str(single.get("display_path") or single["filename"])
            if single["state"] == "downloaded" and not single.get("safe_file_path"):
                detail += "\n" + tr("檔案已不存在或不在原儲存目錄內。")
            if single.get("error"):
                detail += "\n" + translate_text(str(single["error"]))
            # 副本結果只是獨立事件；原紀錄與開啟按鈕仍指向原檔，避免偷偷改變下載路徑。
            # A copy is a separate event; keep the canonical record and Open actions on the original.
            event = single.get("latest_redownload")
            if isinstance(event, dict):
                mode = tr("另存副本") if event.get("mode") == "copy" else tr("覆寫原檔")
                detail += "\n" + tr("最近重新下載：{mode} · {state}", mode=mode, state=self._state(event.get("state")))
                if event.get("mode") == "copy" and event.get("state") == "downloaded" and event.get("path"):
                    detail += "\n" + tr("副本：{filename}", filename=Path(str(event["path"])).name)
                if event.get("recovery_path"):
                    detail += "\n" + tr("原檔備份：{filename}", filename=Path(str(event["recovery_path"])).name)
                if event.get("error"):
                    detail += "\n" + translate_text(str(event["error"]))
            self.detail_label.setText(detail[:800])
            self.detail_label.setToolTip(_tip(str(single.get("path") or single["filename"]) + "\n" + detail))
        else:
            self.detail_label.setText(tr("選取已完成項目可重新下載；失敗項目可重試。"))
            self.detail_label.setToolTip("")
        self.message_label.setText(translate_text(self._last_error) if self._last_error else tr("正在讀取紀錄…") if self._loading else tr("沒有符合條件的紀錄。") if self._section() == 0 and not self._rows else tr("尚無收集工作。") if self._section() == 1 and not self._jobs else "")

    def set_retry_enabled(self, enabled):
        self._retry_allowed = bool(enabled)
        self._update_controls()

    def _retry_selected(self):
        if self._retry_allowed and not self._loading:
            identifiers = list(dict.fromkeys(row["media_key"] for row in self._selected_media() if row["state"] == "failed"))
            if identifiers:
                self.retryRequested.emit(identifiers)

    def _redownload_selected(self):
        # 先固定目前選取的已完成項目，再讓使用者選擇另存或覆寫；視窗本身不改紀錄。
        # Snapshot selected completed IDs before choosing copy/replace; this view never edits records.
        if not self._retry_allowed or self._loading or self._section() != 0:
            return
        identifiers = list(dict.fromkeys(row["media_key"] for row in self._selected_media() if row["state"] == "downloaded"))
        if not identifiers:
            return
        mode = choose_redownload_mode(len(identifiers), parent=self.window(), theme=self.theme_mode)
        # 巢狀確認期間主視窗仍可能結束背景工作，因此再次檢查可執行狀態。
        # Recheck the busy gate after the nested choice dialog before starting a worker.
        if mode in {"copy", "overwrite"} and self._retry_allowed and not self._loading and not self._hidden:
            self.redownloadRequested.emit(identifiers, mode)

    def _open_local(self, kind):
        # 列表中的路徑只是快照，點按時必須重新查驗檔案與原目錄邊界。
        # Listed paths are snapshots; recheck file existence and saved-directory boundaries on click.
        selected = self._selected_media()
        if len(selected) != 1:
            return
        try:
            path = storage.history_open_target(self.db_path, selected[0]["media_key"], kind)
        except (OSError, RuntimeError, ValueError):
            path = None
        if path is None:
            self._last_error = "檔案已不存在或不在原儲存目錄內。"
            self._update_controls()
        else:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self._last_error = "無法開啟此項目，請確認檔案或預設應用程式。"
                self._update_controls()

    def _open_post(self):
        selected = self._selected_media()
        if len(selected) == 1 and re.fullmatch(r"[0-9]{1,30}", str(selected[0]["post_id"])):
            if not QDesktopServices.openUrl(QUrl("https://x.com/i/web/status/"+str(selected[0]["post_id"]))):
                self._last_error = "無法開啟連結，請檢查預設瀏覽器。"
                self._update_controls()

    def refresh_language(self):
        # 重用既有輸入與表格，避免翻譯動作清空搜尋條件或選取的媒體。
        # Reuse inputs and tables so translation preserves the query and media selection.
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("下載紀錄"))
        self.title_label.setText(tr("下載紀錄"))
        if self.tabs is not None:
            self.tabs.setTabText(0, tr("紀錄與圖片預覽"))
            self.tabs.setTabText(1, tr("詳細下載狀況"))
        else:
            self.progress_toggle.setText(tr("工作與來源進度"))
        self.session_log_hint.setText(tr("本次執行紀錄（最多保留 2000 筆）"))
        self.session_log.setAccessibleName(tr("詳細下載狀況"))
        self.session_log.setPlaceholderText(tr("開始後，會在這裡顯示完整下載狀況。"))
        self._render_session_log()
        self.status_label.setText(tr("狀態"))
        self.search_label.setText(tr("搜尋"))
        for index, text in enumerate(("全部","已完成","失敗")):
            self.status_combo.setItemText(index, tr(text))
        self.search_input.setPlaceholderText(tr("作者帳號或檔名"))
        self.search_input.setAccessibleName(tr("搜尋作者帳號或檔名"))
        for button, source in ((self.refresh_button,"重新整理"),(self.file_button,"開啟檔案"),(self.folder_button,"開啟資料夾"),(self.post_button,"開啟來源貼文"),(self.previous_button,"上一頁"),(self.next_button,"下一頁"),(self.retry_button,"重試選取的失敗項目"),(self.redownload_button,"重新下載選取項目"),(self.close_button,"關閉")):
            button.setText(tr(source))
        self.redownload_button.setToolTip(tr("只處理選取的已完成項目；失敗項目請使用重試。"))
        self.history_table.setHorizontalHeaderLabels([tr(value) for value in ("狀態","作者","檔名","更新時間")])
        self.jobs_table.setHorizontalHeaderLabels([tr(value) for value in ("狀態","目標標籤","來源完成進度","更新時間")])
        self.sources_table.setHorizontalHeaderLabels([tr(value) for value in ("類型","來源","狀態","進度","訊息")])
        self.jobs_hint.setText(tr("顯示最近 20 次收集工作；來源完成不代表已收齊 X 的所有作品。"))
        self.source_hint.setText(tr("選取工作可查看來源進度；最多顯示 200 個來源，未完成項目優先。"))
        self._render_history()
        self._render_jobs()
        self._render_sources()
        self._update_controls()

    retranslate_ui = refresh_language

    def set_theme(self, theme):
        self.theme_mode = "dark" if theme == "dark" else "light"
        apply_dialog_theme(self, self.theme_mode)
        self.setObjectName("historyDialog")
        colors = theme_colors(self.theme_mode)
        # QWidget 面板與捲動內容會保留建立時的 palette，切換主題時需一併更新。
        # Embedded QWidget surfaces and scroll contents retain construction palettes; update them with the panel theme.
        for surface in (self, *self.findChildren(QWidget)):
            if type(surface) is QWidget or isinstance(surface, (QScrollArea, QSplitter)):
                apply_palette(surface, self.theme_mode)
        self.setStyleSheet(self.styleSheet()+f"""
            QWidget#historyDialog {{ background:{colors['window']}; color:{colors['ink']}; }}
            QTableWidget {{ background:{colors['input']}; alternate-background-color:{colors['surface']};
                color:{colors['ink']}; border:1px solid {colors['border']}; border-radius:7px;
                selection-background-color:{colors['accent_soft']}; selection-color:{colors['accent_soft_ink']}; }}
            QTableWidget::item {{ padding:3px 6px; }}
            QHeaderView::section {{ background:{colors['surface']}; color:{colors['ink']};
                border:0px; border-bottom:1px solid {colors['divider']}; padding:7px; }}
            QTableCornerButton::section {{ background:{colors['surface']}; border:none; }}
            QTabWidget::pane {{ border:none; }}
            QTabBar::tab {{ background:{colors['surface']}; color:{colors['ink']}; padding:9px 14px;
                border-bottom:2px solid transparent; }}
            QTabBar::tab:selected {{ background:{colors['accent_soft']}; color:{colors['accent_soft_ink']};
                border-bottom-color:{colors['accent']}; }}
        """)
        self._refresh_preview_widgets()

    def showEvent(self, event):
        super().showEvent(event)
        self._hidden = False
        self.refresh()

    def _cancel_queries(self):
        # 不強制終止 SQLite 執行緒；讓有限查詢完成，隱藏期間忽略其結果。
        # Let bounded SQLite queries finish naturally and ignore their results while hidden.
        self._hidden = True
        self._generation += 1
        self._pending = None
        self._loading = False
        self._debounce.stop()
        self._live_refresh.stop()
        self._cancel_previews()
        self._refresh_preview_widgets()

    def hideEvent(self, event):
        self._cancel_queries()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._cancel_queries()
        super().closeEvent(event)


class HistoryDialog(QDialog):
    """Compatibility wrapper for callers that still need a standalone native dialog."""
    retryRequested = pyqtSignal(object)
    redownloadRequested = pyqtSignal(object, str)

    def __init__(self, parent=None, db_path=None, theme="light"):
        super().__init__(parent)
        self.setProperty("i18nSkip", True)
        self.setWindowIcon(QIcon(get_icon_path()))
        self.resize(1040, 740)
        self.setMinimumSize(800, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = HistoryPanel(self, db_path, theme, embedded=False)
        layout.addWidget(self.panel)
        self.panel.retryRequested.connect(self.retryRequested)
        self.panel.redownloadRequested.connect(self.redownloadRequested)
        self.panel.close_button.clicked.disconnect()
        self.panel.close_button.clicked.connect(self.close)
        self.set_theme(theme)
        self.refresh_language()

    def __getattr__(self, name):
        panel = self.__dict__.get("panel")
        if panel is not None:
            return getattr(panel, name)
        raise AttributeError(name)

    @property
    def _pool(self):
        return self.panel._pool

    @_pool.setter
    def _pool(self, pool):
        self.panel._pool = pool

    def set_theme(self, theme):
        apply_dialog_theme(self, theme)
        self.panel.set_theme(theme)

    def refresh_language(self):
        self.setWindowTitle(tr("下載紀錄"))
        self.panel.refresh_language()

    retranslate_ui = refresh_language
