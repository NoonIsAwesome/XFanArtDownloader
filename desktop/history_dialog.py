"""下載紀錄與收集進度。Read-only native history and resumable-source views."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
import re

from PyQt6.QtCore import QDateTime, QItemSelectionModel, QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import storage
from i18n import tr, translate_text, locale as ui_locale
from resources_rc import get_icon_path
from ui_theme import apply_dialog_theme, theme_colors


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


class HistoryDialog(QDialog):
    retryRequested = pyqtSignal(object)

    def __init__(self, parent=None, db_path=None, theme="light"):
        super().__init__(parent)
        if db_path is None:
            raise ValueError("HistoryDialog requires a database path")
        self.db_path = Path(db_path)
        self.theme_mode = theme
        self.setProperty("i18nSkip", True)
        self.setWindowIcon(QIcon(get_icon_path()))
        self.resize(1040, 740)
        self.setMinimumSize(800, 590)
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._active = self._pending = None
        self._loading = False
        self._hidden = False
        self._retry_allowed = True
        self._page, self._total = 1, 0
        self._rows, self._jobs, self._sources = [], [], []
        self._last_error = ""
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._new_search)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        self.title_label = _label()
        self.title_label.setProperty("role", "dialogTitle")
        layout.addWidget(self.title_label)
        self.tabs = QTabWidget()
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
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        body.addWidget(self.history_table, 1)
        self.detail_label = _label(muted=True)
        self.detail_label.setMaximumHeight(64)
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
        self.tabs.addTab(history, "")

        # 工作頁顯示實際來源狀態，不把搜尋跑完等同於已收齊 X 的所有作品。
        # Show recorded source states without equating completed searches with exhaustive X coverage.
        progress = QWidget()
        progress_layout = QVBoxLayout(progress)
        progress_layout.setContentsMargins(0, 14, 0, 0)
        progress_layout.setSpacing(10)
        self.jobs_hint = _label(muted=True)
        progress_layout.addWidget(self.jobs_hint)
        self.jobs_table = _table(4)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_table.setMaximumHeight(190)
        self.jobs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        progress_layout.addWidget(self.jobs_table)
        self.source_hint = _label(muted=True)
        progress_layout.addWidget(self.source_hint)
        self.sources_table = _table(5)
        self.sources_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sources_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sources_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        progress_layout.addWidget(self.sources_table, 1)
        self.tabs.addTab(progress, "")
        self.message_label = _label(muted=True)
        layout.addWidget(self.message_label)
        footer = QHBoxLayout()
        self.retry_button, self.close_button = QPushButton(), QPushButton()
        self.retry_button.setProperty("variant", "primary")
        footer.addWidget(self.retry_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
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
        self.tabs.currentChanged.connect(self.refresh)
        self.retry_button.clicked.connect(self._retry_selected)
        self.file_button.clicked.connect(lambda:self._open_local("file"))
        self.folder_button.clicked.connect(lambda:self._open_local("folder"))
        self.post_button.clicked.connect(self._open_post)
        self.close_button.clicked.connect(self.close)
        self.set_theme(theme)
        self.refresh_language()

    def _request(self, kind, **arguments):
        self._generation += 1
        self._loading, self._last_error = True, ""
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
                        self.jobs_table.selectRow(0)
                else:
                    self._sources = result
                    self._render_sources()
            self._update_controls()
        if not self._hidden and pending is not None and pending[0] == self._generation:
            self._start_query(pending)

    def refresh(self, *_):
        if self.tabs.currentIndex() == 0:
            self._request("history", status=self.status_combo.currentData(), search=self.search_input.text(), page=self._page, page_size=40)
        else:
            self._request("jobs", limit=20)

    def _new_search(self, *_):
        self._debounce.stop()
        self._page = 1
        if self.tabs.currentIndex() == 0:
            self.refresh()

    def _change_page(self, delta):
        self._page = max(1, self._page + delta)
        self.refresh()

    def _selected_media(self):
        return [self._rows[index.row()] for index in self.history_table.selectionModel().selectedRows()
                if index.row() < len(self._rows)]

    def _selection_changed(self):
        self._update_controls()

    def _job_selected(self):
        indices = self.jobs_table.selectionModel().selectedRows()
        if indices and indices[0].row() < len(self._jobs):
            self._sources = []
            self._render_sources()
            self._request("sources", job_id=self._jobs[indices[0].row()]["job_id"], limit=200)

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
        self.retry_button.setVisible(self.tabs.currentIndex() == 0)
        self.retry_button.setEnabled(self._retry_allowed and not self._loading and any(row["state"] == "failed" for row in selected))
        self.file_button.setEnabled(bool(single and single.get("safe_file_path")))
        self.folder_button.setEnabled(bool(single and single.get("safe_folder_path")))
        self.post_button.setEnabled(bool(single and re.fullmatch(r"[0-9]{1,30}", str(single["post_id"]))))
        self.previous_button.setEnabled(not self._loading and self._page > 1)
        self.next_button.setEnabled(not self._loading and self._page * 40 < self._total)
        self.page_label.setText(tr("第 {page} / {pages} 頁 · 共 {total} 個媒體",page=self._page,pages=max(1,(self._total+39)//40),total=self._total))
        if single:
            detail = str(single.get("path") or single["filename"])
            if single["state"] == "downloaded" and not single.get("safe_file_path"):
                detail += "\n" + tr("檔案已不存在或不在原儲存目錄內。")
            if single.get("error"):
                detail += "\n" + translate_text(str(single["error"]))
            self.detail_label.setText(detail[:800])
            self.detail_label.setToolTip(_tip(detail))
        else:
            self.detail_label.setText(tr("選取一筆紀錄可查看檔案；可多選失敗項目重試。"))
            self.detail_label.setToolTip("")
        self.message_label.setText(translate_text(self._last_error) if self._last_error else tr("正在讀取紀錄…") if self._loading else tr("沒有符合條件的紀錄。") if self.tabs.currentIndex() == 0 and not self._rows else tr("尚無收集工作。") if self.tabs.currentIndex() == 1 and not self._jobs else "")

    def set_retry_enabled(self, enabled):
        self._retry_allowed = bool(enabled)
        self._update_controls()

    def _retry_selected(self):
        if self._retry_allowed and not self._loading:
            identifiers = list(dict.fromkeys(row["media_key"] for row in self._selected_media() if row["state"] == "failed"))
            if identifiers:
                self.retryRequested.emit(identifiers)

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
        self.tabs.setTabText(0, tr("下載紀錄"))
        self.tabs.setTabText(1, tr("收集進度"))
        self.status_label.setText(tr("狀態"))
        self.search_label.setText(tr("搜尋"))
        for index, text in enumerate(("全部","已完成","失敗")):
            self.status_combo.setItemText(index, tr(text))
        self.search_input.setPlaceholderText(tr("作者帳號或檔名"))
        self.search_input.setAccessibleName(tr("搜尋作者帳號或檔名"))
        for button, source in ((self.refresh_button,"重新整理"),(self.file_button,"開啟檔案"),(self.folder_button,"開啟資料夾"),(self.post_button,"開啟來源貼文"),(self.previous_button,"上一頁"),(self.next_button,"下一頁"),(self.retry_button,"重試選取的失敗項目"),(self.close_button,"關閉")):
            button.setText(tr(source))
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
        self.setStyleSheet(self.styleSheet()+f"""
            QDialog#historyDialog {{ background:{colors['window']}; }}
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

    def closeEvent(self, event):
        self._cancel_queries()
        super().closeEvent(event)

    def reject(self):
        self._cancel_queries()
        super().reject()
