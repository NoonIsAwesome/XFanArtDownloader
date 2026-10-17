"""Gallery collection workbench backed by gallery-dl and durable history."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit

from PyQt6.QtCore import QDate, QEvent, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCloseEvent, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDateEdit, QFileDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTextEdit, QWidget,
)

from author_dialog import AuthorInfoDialog
from downloader import DownloadEngine, DownloadOptions, parse_hashtags
from login_browser import LoginBrowser, import_cookies
from resources_rc import get_icon_path
from ui_design import apply_dialog_theme, build_gallery_layout, refresh_gallery_summary, set_window_theme, WrappingCheckBox
from gallery_widgets import GalleryDateEdit, UserNoticeDialog
from help_dialog import UsageGuideDialog
from i18n import (
    tr, translate_text, set_language, language_mode, locale as ui_locale,
    translate_widget_tree, install_qt_translator,
)


def application_dir() -> Path:
    """Keep data at the project root for source runs, or beside a standalone EXE."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        project_dir = executable_dir.parent
        if executable_dir.name.casefold() == "dist" and (project_dir / "desktop" / "main.py").is_file():
            return project_dir
        return executable_dir
    return Path(__file__).resolve().parents[1]


def normalized_hashtag(value: str) -> str:
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    if not value or any(char != "_" and unicodedata.category(char)[0] not in "LNM" for char in value):
        raise ValueError(tr('請輸入完整 Hashtag，例如 #角色名稱；多個標籤請用空白或逗號分隔。'))
    return "#" + value


def normalized_author(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "x.com", "www.x.com", "twitter.com", "www.twitter.com",
        ):
            raise ValueError(tr('作者網址須為 X 或 Twitter 的作者頁：{value}', value=value))
        pieces = parsed.path.strip("/").split("/")
        if len(pieces) not in (1, 2) or (len(pieces) == 2 and pieces[1] != "media"):
            raise ValueError(tr('請填作者帳號或作者媒體頁，貼文網址請放入下方欄位：{value}', value=value))
        value = pieces[0]
    else:
        value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) or value.lower() in (
        "home", "explore", "search", "settings", "notifications", "messages", "i",
    ):
        raise ValueError(tr('作者帳號格式不正確：{value}；請填 @帳號或作者網址。', value=value or tr('空白')))
    return value


def normalized_tweet_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (
        "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    ):
        raise ValueError(tr('請填完整 X / Twitter 貼文網址：{value}', value=value))
    match = re.fullmatch(
        r"(/(?:[A-Za-z0-9_]{1,15}/status|i/web/status|i/status)/\d+)(?:/(?:photo|video)/\d+)?/?",
        parsed.path,
    )
    if not match:
        raise ValueError(tr('找不到貼文編號，請確認貼文網址：{value}', value=value))
    return "https://x.com" + match.group(1)


class DownloadWorker(QObject):
    message = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, options: DownloadOptions, stop_event: threading.Event):
        super().__init__()
        self.options = options
        self.stop_event = stop_event

    @pyqtSlot()
    def run(self):
        try:
            result = DownloadEngine(self.options, log=self.message.emit, stop_event=self.stop_event).run()
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class LoginWorker(QObject):
    """All browser calls stay in one thread, including browser shutdown."""

    completed = pyqtSignal(str, int)
    failed = pyqtSignal(str, str)
    progress = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, profile_dir: Path, cookie_file: Path):
        super().__init__()
        self.profile_dir = profile_dir
        self.cookie_file = cookie_file
        self.browser = None

    def _browser(self):
        # The existing app-owned profile can be saved after an app restart,
        # without opening another interactive login window first.
        if self.browser is None:
            self.browser = LoginBrowser(self.profile_dir, on_progress=self.progress.emit)
        return self.browser

    @pyqtSlot()
    def open_browser(self):
        try:
            self._browser().open()
            self.completed.emit("open", 0)
        except Exception as exc:
            self.failed.emit("open", str(exc))

    @pyqtSlot()
    def save_cookies(self):
        try:
            count = self._browser().save_cookies(self.cookie_file)
            if count <= 0:
                raise RuntimeError("未取得 X Cookie；請先完成登入。")
            self.completed.emit("save", count)
        except Exception as exc:
            self.failed.emit("save", str(exc))

    @pyqtSlot()
    def close_browser(self):
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception as exc:
            self.failed.emit("close", str(exc))
        else:
            self.browser = None
            self.closed.emit()


class DriverWorker(QObject):
    message = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = base_dir

    @pyqtSlot()
    def run(self):
        try:
            from driver_manager import ensure_chromedriver
            version = ensure_chromedriver(self.base_dir, log=self.message.emit)
            self.message.emit(f"✅ ChromeDriver 檢查完成：{version}")
        except Exception as exc:
            self.message.emit(f"⚠️ ChromeDriver 檢查失敗：{exc}；新版下載不需要此驅動，可繼續使用。")
        finally:
            self.finished.emit()


class TwitterDownloader(QWidget):
    browser_open_requested = pyqtSignal()
    cookie_save_requested = pyqtSignal()
    browser_close_requested = pyqtSignal()

    def __init__(self, show_notice: bool = True, base_dir: Path | None = None):
        super().__init__()
        self.base_dir = Path(base_dir) if base_dir is not None else application_dir()
        self.data_dir = self.base_dir / "data"
        self.cookie_file = self.data_dir / "cookies.txt"
        self.settings_file = self.data_dir / "settings.json"
        # Read just the language before creating widgets or first-run guidance.
        # Full settings validation and its visible errors still happen below.
        initial_language = "system"
        try:
            if self.settings_file.is_file() and self.settings_file.stat().st_size <= 1024 * 1024:
                initial_settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if isinstance(initial_settings, dict):
                    initial_language = initial_settings.get("language", "system")
        except (OSError, ValueError, TypeError):
            pass
        set_language(initial_language)
        self.language_mode = language_mode()
        install_qt_translator(QApplication.instance())
        self.setLocale(ui_locale())
        self.download_path = str(self.base_dir / "downloads")
        self.successful_downloads = 0
        self.theme_mode = "light"
        self._run_state = "尚未開始"
        self.is_downloading = False
        self._show_notice = show_notice
        self._download_thread = None
        self._download_worker = None
        self._stop_event = threading.Event()
        self._login_thread = None
        self._login_worker = None
        self._login_busy = False
        self._login_browser_open = False
        self._login_operation = ""
        self._login_progress_message = ""
        self._login_status_message = ""
        self._login_shutdown_requested = False
        self._driver_thread = None
        self._driver_worker = None
        self._close_pending = False
        self._allow_close = False
        self._usage_guide = None
        self._usage_guide_seen = False
        self._status_entries = []
        self._result_detail = None

        self.setWindowIcon(QIcon(get_icon_path()))
        self.setWindowTitle("FanArt Downloader")

        self.hashtag_input = QLineEdit("")
        self.hashtag_input.setAccessibleName(tr('目標標籤，可輸入多個，以空白或逗號分隔'))
        self.cookie_btn = QPushButton(tr('開啟瀏覽器登入'))
        self.cookie_save_btn = QPushButton(tr('儲存登入狀態'))
        self.cookie_import_btn = QPushButton(tr('匯入 Cookie 檔'))
        self.choose_path_btn = QPushButton(tr('選擇儲存位置'))
        self.path_label = QLabel(self.download_path)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.folder_by_user_checkbox = WrappingCheckBox(tr('為每一位作者建立獨立資料夾'))
        self.folder_by_user_checkbox.setChecked(True)
        self.download_btn = QPushButton(tr('3️⃣ 開始下載'))
        self.stop_btn = QPushButton(tr('⏹️ 停止下載'))
        self.stop_btn.setEnabled(False)
        self.status_box = QTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setAccessibleName(tr('下載狀態與錯誤訊息'))
        self.status_box.setMinimumHeight(100)
        self.status_box.document().setMaximumBlockCount(2000)

        self.search_checkbox = QCheckBox(tr('搜尋 Hashtag 貼文'))
        self.search_checkbox.setChecked(True)
        self.author_media_checkbox = QCheckBox(tr('從作者媒體頁補抓相同 Hashtag'))
        self.author_media_checkbox.setChecked(True)
        self.authors_input = QTextEdit()
        self.authors_input.setAcceptRichText(False)
        self.authors_input.setPlaceholderText(tr('每行一位作者：@帳號 或 https://x.com/帳號/media'))
        self.authors_input.setAccessibleName(tr('補抓作者，每行一位'))
        self.authors_input.setMaximumHeight(65)
        self.discover_authors_checkbox = QCheckBox(tr('補抓歷史與本次搜尋已發現的作者'))
        self.discover_authors_checkbox.setChecked(True)
        self.tweet_urls_input = QTextEdit()
        self.tweet_urls_input.setAcceptRichText(False)
        self.tweet_urls_input.setPlaceholderText(tr('選填；每行一則完整貼文網址：https://x.com/帳號/status/編號'))
        self.tweet_urls_input.setAccessibleName(tr('貼文網址，每行一則'))
        self.tweet_urls_input.setMaximumHeight(65)
        self.retry_failed_checkbox = WrappingCheckBox(tr('重試先前失敗的下載'))
        self.retry_failed_checkbox.setChecked(True)
        self.reply_media_checkbox = WrappingCheckBox(tr("補抓原作者在同一討論串的圖片與影片"))
        self.reply_media_checkbox.setChecked(True)
        self.auto_driver_checkbox = WrappingCheckBox(tr('啟動時自動檢查 ChromeDriver'))
        self.auto_driver_checkbox.setChecked(True)
        self.auto_driver_checkbox.setToolTip(tr('新版下載不需要此驅動；此功能保留舊版驅動的更新能力。'))
        self.driver_check_btn = QPushButton(tr('檢查驅動更新'))
        self.driver_check_btn.setToolTip(tr('新版下載不需要此驅動。'))
        self.warp_proxy_checkbox = WrappingCheckBox(tr("透過 WARP 本機代理連線（僅本軟體）"))
        self.warp_proxy_port_input = QSpinBox()
        self.warp_proxy_port_input.setRange(1, 65535)
        self.warp_proxy_port_input.setValue(40000)
        self.warp_proxy_port_input.setAccessibleName(tr("本機代理連接埠"))

        self.limit_count_checkbox = WrappingCheckBox(tr('限制處理貼文數（含已下載）'))
        self.limit_count_input = QLineEdit("100")
        self.limit_count_input.setAccessibleName(tr('本輪處理貼文上限'))
        self.limit_date_checkbox = WrappingCheckBox(tr('只處理此日期起的貼文'))
        self.limit_date_input = GalleryDateEdit(theme=self.theme_mode)
        self.limit_date_input.setDisplayFormat("yyyy-MM-dd")
        self.limit_date_input.setCalendarPopup(True)
        self.limit_date_input.setDate(QDate.currentDate())
        self.limit_date_input.setAccessibleName(tr('貼文起始日期'))
        self.human_behavior_checkbox = WrappingCheckBox(tr('下載間隨機休息（假裝自己不是機器人）'))
        self.human_behavior_checkbox.setChecked(True)

        self.setup_ui()
        self.setup_signals()
        self._load_settings()
        self._update_controls()
        self.refresh_language()
        if show_notice:
            if not self._usage_guide_seen:
                QTimer.singleShot(0, self.show_usage_guide)
            QTimer.singleShot(0, self._auto_check_driver)

    def setup_ui(self):
        build_gallery_layout(self)

    def setup_signals(self):
        self.cookie_btn.clicked.connect(self.open_browser)
        self.cookie_save_btn.clicked.connect(self.save_cookies)
        self.cookie_import_btn.clicked.connect(self.import_cookie_file)
        self.choose_path_btn.clicked.connect(self.select_download_path)
        self.download_btn.clicked.connect(self.start_download_thread)
        self.stop_btn.clicked.connect(self.stop_download)
        self.driver_check_btn.clicked.connect(self.check_driver_update)
        self.author_media_checkbox.toggled.connect(self._update_controls)
        self.limit_count_checkbox.toggled.connect(self._update_controls)
        self.limit_date_checkbox.toggled.connect(self._update_controls)
        self.warp_proxy_checkbox.toggled.connect(self._update_controls)
        self.hashtag_input.textChanged.connect(self._refresh_summary)
        self.search_checkbox.toggled.connect(self._refresh_summary)
        self.discover_authors_checkbox.toggled.connect(self._refresh_summary)
        self.retry_failed_checkbox.toggled.connect(self._refresh_summary)
        self.reply_media_checkbox.toggled.connect(self._refresh_summary)
        self.authors_input.textChanged.connect(self._refresh_summary)
        self.tweet_urls_input.textChanged.connect(self._refresh_summary)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.about_btn.clicked.connect(self.show_author_info)
        self.help_btn.clicked.connect(self.show_usage_guide)
        self.language_combo.currentIndexChanged.connect(self._language_selected)

    def _language_selected(self, index):
        code = self.language_combo.itemData(index)
        if code == self.language_mode:
            return
        set_language(code)
        self.language_mode = language_mode()
        self.refresh_language()
        try:
            self._save_settings()
        except OSError as exc:
            self._log("⚠️ 無法保存設定：{exc}", exc=exc)

    def refresh_language(self):
        """Retranslate in place: inputs, worker connections and state survive."""
        install_qt_translator(QApplication.instance())
        self.setLocale(ui_locale())
        translate_widget_tree(self)
        self.language_combo.blockSignals(True)
        self.language_combo.setItemText(0, tr("跟隨電腦設定"))
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language_mode)))
        self.language_combo.blockSignals(False)
        self.language_combo.setAccessibleName(tr("介面語言"))
        self.language_combo.setToolTip(tr("選擇介面語言；系統選項會跟隨電腦設定。"))
        # Theme-dependent labels are generated afresh from canonical text.
        set_window_theme(self, self.theme_mode)
        self._refresh_summary()
        if self._result_detail is not None:
            template, values = self._result_detail
            self.result_detail_label.setText(tr(template, **values))
        translated_log = "\n".join(self._render_status(*entry) for entry in self._status_entries)
        self.status_box.setPlainText(translated_log + ("\n" if self._status_entries else ""))
        self.status_box.moveCursor(QTextCursor.MoveOperation.End)
        for child in self.findChildren(QWidget):
            if hasattr(child, "retranslate_ui"):
                child.retranslate_ui()
            elif hasattr(child, "refresh_language"):
                child.refresh_language()
        self.limit_date_input.setLocale(ui_locale())

    def _set_result_detail(self, template, **values):
        self._result_detail = (template, values)
        self.result_detail_label.setText(tr(template, **values))

    @staticmethod
    def _render_status(message, values):
        if values is None:
            return translate_text(message)
        # Paths, handles and tags remain literal; only backend error prose and
        # the canonical run-state fragment need a second translation boundary.
        arguments = {key: translate_text(str(value)) if key in {"exc", "message", "state", "operation"} else value
                     for key, value in values.items()}
        return tr(message, **arguments)

    def _log(self, template, **values):
        self._status_entries.append((template, values))
        del self._status_entries[:-2000]
        self._append_status_text(self._render_status(template, values))

    def toggle_theme(self):
        set_window_theme(self, "dark" if self.theme_mode == "light" else "light")
        if self._usage_guide is not None:
            self._usage_guide.set_theme(self.theme_mode)
        try:
            self._save_settings()
        except OSError as exc:
            self._log('⚠️ 無法保存主題設定：{exc}', exc=exc)

    def show_author_info(self):
        AuthorInfoDialog(self, theme=self.theme_mode).exec()

    def _refresh_summary(self, *_):
        refresh_gallery_summary(self)
        self.run_state_label.setText(translate_text(self._run_state))
        if self._login_busy:
            login_status = self._login_progress_message or (
                "正在開啟專用登入視窗…" if self._login_operation == "open" else "正在讀取並儲存 X 登入狀態…"
            )
        elif self._login_status_message:
            login_status = self._login_status_message
        elif self._login_browser_open:
            login_status = "專用登入視窗已開啟；完成後先關窗再儲存。"
        else:
            login_status = "已儲存登入資料" if self.cookie_file.is_file() else "尚未儲存登入資料"
        self.login_state_label.setText(translate_text(login_status))

    def show_user_notice(self):
        UserNoticeDialog(self, theme=self.theme_mode).exec()

    @pyqtSlot(str)
    def append_status(self, message: str):
        self._status_entries.append((str(message), None))
        del self._status_entries[:-2000]
        self._append_status_text(translate_text(str(message)))

    def _append_status_text(self, message):
        # Post text and filenames may contain markup; always display plain text.
        cursor = self.status_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(str(message) + "\n")
        self.status_box.setTextCursor(cursor)
        self.status_box.ensureCursorVisible()

    def _update_controls(self, *_):
        editable = not self.is_downloading and not self._close_pending
        for widget in (
            self.hashtag_input, self.choose_path_btn, self.search_checkbox,
            self.author_media_checkbox, self.tweet_urls_input, self.retry_failed_checkbox,
            self.folder_by_user_checkbox, self.human_behavior_checkbox,
            self.reply_media_checkbox,
            self.limit_count_checkbox, self.limit_date_checkbox,
            self.warp_proxy_checkbox,
        ):
            widget.setEnabled(editable)
        author_enabled = editable and self.author_media_checkbox.isChecked()
        self.authors_input.setEnabled(author_enabled)
        self.discover_authors_checkbox.setEnabled(author_enabled)
        self.limit_count_input.setEnabled(editable and self.limit_count_checkbox.isChecked())
        self.limit_date_input.setEnabled(editable and self.limit_date_checkbox.isChecked())
        self.warp_proxy_port_input.setEnabled(editable and self.warp_proxy_checkbox.isChecked())
        self.download_btn.setEnabled(editable and not self._login_busy)
        self.cookie_btn.setEnabled(editable and not self._login_busy)
        self.cookie_save_btn.setEnabled(editable and not self._login_busy)
        self.cookie_import_btn.setEnabled(editable and not self._login_busy)
        self.stop_btn.setEnabled(self.is_downloading and not self._stop_event.is_set())
        self.auto_driver_checkbox.setEnabled(not self._close_pending)
        self.driver_check_btn.setEnabled(self._driver_thread is None and not self._close_pending)
        self.language_combo.setEnabled(not self._close_pending)
        self._refresh_summary()

    def snapshot_options(self) -> DownloadOptions:
        """Read widgets once on the GUI thread; workers only receive this value."""
        hashtags = parse_hashtags(self.hashtag_input.text())
        # Keep the legacy field for older callers and a readable single-tag label.
        # The engine uses the normalized tuple for matching every source.
        first_token = next(token for token in re.split(r"[\s,，;；]+", self.hashtag_input.text()) if token)
        hashtag = normalized_hashtag(first_token.replace("＃", "#", 1))
        limit_count = None
        if self.limit_count_checkbox.isChecked():
            try:
                limit_count = int(self.limit_count_input.text().strip())
            except ValueError as exc:
                raise ValueError(tr('限制處理貼文數必須是大於 0 的整數。')) from exc
            if limit_count <= 0:
                raise ValueError(tr('限制處理貼文數必須是大於 0 的整數。'))
        authors = ()
        discover_authors = False
        if self.author_media_checkbox.isChecked():
            authors = tuple(dict.fromkeys(normalized_author(line) for line in self.authors_input.toPlainText().splitlines() if line.strip()))
            discover_authors = self.discover_authors_checkbox.isChecked()
        tweet_urls = tuple(dict.fromkeys(normalized_tweet_url(line) for line in self.tweet_urls_input.toPlainText().splitlines() if line.strip()))
        if not (self.search_checkbox.isChecked() or authors or discover_authors or tweet_urls or self.retry_failed_checkbox.isChecked()):
            raise ValueError(tr('請選擇 Hashtag 搜尋、作者補抓、重試失敗，或填入貼文網址。'))
        output_dir = Path(self.download_path).expanduser()
        if output_dir.exists() and not output_dir.is_dir():
            raise ValueError(tr('下載位置是檔案，請重新選擇資料夾。'))
        return DownloadOptions(
            hashtag=hashtag, output_dir=output_dir, database_path=self.data_dir / "downloads.sqlite3",
            cookie_file=self.cookie_file if self.cookie_file.is_file() else None,
            authors=authors, tweet_urls=tweet_urls, search_enabled=self.search_checkbox.isChecked(),
            discover_authors=discover_authors, folder_by_user=self.folder_by_user_checkbox.isChecked(),
            limit_count=limit_count,
            since=self.limit_date_input.date().toPyDate() if self.limit_date_checkbox.isChecked() else None,
            delay=self.human_behavior_checkbox.isChecked(), retry_failed=self.retry_failed_checkbox.isChecked(),
            hashtags=hashtags,
            warp_proxy=self.warp_proxy_checkbox.isChecked(),
            warp_proxy_port=self.warp_proxy_port_input.value(),
            include_author_replies=self.reply_media_checkbox.isChecked(),
        )

    def _validation_error(self, message: str):
        self._log("⚠️ {message}", message=message)
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr('請檢查下載設定'))
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(translate_text(message))
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        apply_dialog_theme(dialog, self.theme_mode)
        dialog.exec()

    def show_usage_guide(self):
        """Show the optional guide without interrupting an active download.

        First launch schedules the same modeless guide once. The toolbar can
        reopen it later, and the saved flag never changes user collection data.
        """
        if self._close_pending:
            return
        if self._usage_guide is None:
            self._usage_guide = UsageGuideDialog(self, theme=self.theme_mode)
        else:
            self._usage_guide.set_theme(self.theme_mode)
        self._usage_guide.show()
        self._usage_guide.raise_()
        self._usage_guide.activateWindow()
        self._usage_guide_seen = True
        try:
            self._save_settings()
        except OSError:
            self._log('無法記住說明視窗偏好；下次仍可從使用說明按鈕開啟。')

    def start_download_thread(self):
        if self.is_downloading or self._login_busy or self._close_pending or self._download_thread is not None:
            return
        try:
            options = self.snapshot_options()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._save_settings()
        except (ValueError, OSError) as exc:
            self._validation_error(str(exc))
            return
        self.successful_downloads = 0
        self.is_downloading = True
        self._run_state = "正在收集"
        self._set_result_detail('正在逐一核對來源與完整標籤；下載紀錄會持續更新。')
        self._stop_event = threading.Event()
        thread = QThread(self)
        worker = DownloadWorker(options, self._stop_event)
        worker.moveToThread(thread)
        self._download_thread = thread
        self._download_worker = worker
        thread.started.connect(worker.run)
        worker.message.connect(self.append_status)
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._download_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._update_controls()
        targets = "、".join("#" + tag for tag in options.hashtags)
        self._log("⏳ 開始處理 {targets}；原貼文須符合任一完整標籤，同串原作者留言依補抓設定處理，重複媒體只下載一次。", targets=targets)
        thread.start()

    @pyqtSlot(object)
    def _download_completed(self, result):
        self.successful_downloads = result.downloaded
        if result.stopped:
            state = "🛑 已停止"
        elif result.errors or result.failed:
            state = "⚠️ 部分完成／有失敗項目" if result.downloaded or result.skipped or result.posts else "❌ 未完成"
        else:
            state = "✅ 本輪處理完成"
        self._run_state = state.split(" ", 1)[-1]
        self._set_result_detail('下載 {downloaded} 個媒體 · 略過 {skipped} · 失敗 {failed}\n處理 {posts} 則貼文 · 標籤／日期不符 {filtered}', downloaded=result.downloaded, skipped=result.skipped, failed=result.failed, posts=result.posts, filtered=result.filtered)
        self._refresh_summary()
        self._log('{state}：下載 {downloaded} 個媒體，略過 {skipped}，失敗 {failed}，標籤／日期不符 {filtered}；處理 {posts} 則貼文。', state=state, downloaded=result.downloaded, skipped=result.skipped, failed=result.failed, filtered=result.filtered, posts=result.posts)
        for error in result.errors[:5]:
            self._log("⚠️ {message}", message=str(error))
        if len(result.errors) > 5:
            self._log('另有 {count} 筆錯誤，請參考上方紀錄。', count=len(result.errors) - 5)

    @pyqtSlot(str)
    def _download_failed(self, message: str):
        self._run_state = "下載未完成"
        self._set_result_detail('請查看下方紀錄，修正問題後可重新開始。')
        self._refresh_summary()
        self._log("❌ 下載未完成：{message}", message=message)

    @pyqtSlot()
    def _download_thread_finished(self):
        self.is_downloading = False
        self._download_thread = None
        self._download_worker = None
        self._update_controls()
        self._maybe_finish_close()

    def stop_download(self):
        if self.is_downloading and not self._stop_event.is_set():
            self._stop_event.set()
            self._run_state = "正在安全停止…"
            self._log('🛑 正在停止；等待目前的連線結束後安全保存下載紀錄。')
            self._update_controls()

    def _ensure_login_worker(self):
        if self._login_thread is not None:
            return
        thread = QThread(self)
        worker = LoginWorker(self.data_dir / "browser-profile", self.cookie_file)
        worker.moveToThread(thread)
        self._login_thread, self._login_worker = thread, worker
        self.browser_open_requested.connect(worker.open_browser)
        self.cookie_save_requested.connect(worker.save_cookies)
        self.browser_close_requested.connect(worker.close_browser)
        worker.completed.connect(self._login_completed)
        worker.failed.connect(self._login_failed)
        worker.progress.connect(self._login_progress)
        worker.closed.connect(thread.quit)
        worker.closed.connect(worker.deleteLater)
        thread.finished.connect(self._login_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def open_browser(self):
        if self.is_downloading or self._login_busy or self._close_pending:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log('❌ 無法建立登入資料夾：{exc}', exc=exc)
            return
        self._ensure_login_worker()
        self._login_busy = True
        self._login_operation = "open"
        self._login_progress_message = ""
        self._login_status_message = ""
        self._update_controls()
        self._log('⏳ 正在以一般模式開啟專用 Chrome／Edge 登入視窗…')
        self.browser_open_requested.emit()

    def save_cookies(self):
        if self.is_downloading or self._login_busy or self._close_pending:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log('❌ 無法建立登入資料夾：{exc}', exc=exc)
            return
        self._ensure_login_worker()
        self._login_busy = True
        self._login_operation = "save"
        self._login_progress_message = ""
        self._login_status_message = ""
        self._update_controls()
        self._log('⏳ 正在讀取專用瀏覽器的 X 登入狀態；完成後會自動關閉擷取視窗，僅保存在本機。')
        self.cookie_save_requested.emit()

    def import_cookie_file(self):
        if self.is_downloading or self._login_busy or self._close_pending:
            return
        source, _ = QFileDialog.getOpenFileName(
            self, tr('匯入 X Cookie（Netscape TXT 或舊版 JSON）'), str(self.base_dir),
            tr('Cookie 檔案 (*.txt *.json);;所有檔案 (*)'),
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not source:
            return
        try:
            count = import_cookies(Path(source), self.cookie_file)
            self._login_status_message = "已匯入登入資料（本機）"
            self._log('✅ 已匯入 {count} 個 Cookie；檔案保存在 data/cookies.txt。', count=count)
            self._refresh_summary()
        except Exception as exc:
            self._log('❌ 匯入失敗：{exc}；請選擇含 X 登入 Cookie 的 Netscape TXT 或舊版 cookies.json。', exc=exc)

    @pyqtSlot(str, int)
    def _login_completed(self, operation: str, count: int):
        self._login_busy = False
        self._login_operation = ""
        self._login_progress_message = ""
        if operation == "open":
            self._login_browser_open = True
            self._login_status_message = ""
            self._log('🔓 請在專用瀏覽器中完成 X 或 Google 登入。登入完成後，請關閉剛才的專用瀏覽器視窗，再回來按儲存。')
        else:
            self._login_browser_open = False
            self._login_status_message = "已儲存登入資料（本機）"
            self._log('✅ 已儲存 {count} 個 Cookie 至 data/cookies.txt。', count=count)
        self._update_controls()
        self._request_login_shutdown()

    @pyqtSlot(str, str)
    def _login_failed(self, operation: str, message: str):
        self._login_busy = False
        self._login_operation = ""
        self._login_progress_message = ""
        self._login_status_message = {
            "save": "儲存未完成，請查看紀錄並重試。",
            "open": "登入視窗未能開啟，請查看紀錄。",
            "close": "結束登入工作時發生問題，請查看紀錄。",
        }.get(operation, "登入操作未完成，請查看紀錄。")
        if operation == "open":
            self._login_browser_open = False
        elif operation == "close":
            # Retain the worker and its browser object so another close attempt
            # can finish cleaning up the exporter it owns.
            self._login_shutdown_requested = False
            self._close_pending = False
        self._log('❌ 登入瀏覽器{operation}失敗：{message}', operation='儲存' if operation == 'save' else '操作', message=message)
        if operation == "save":
            self._log('請先關閉剛才的專用瀏覽器視窗，再按「儲存登入狀態」重試；需要外部 Cookie 檔時，請另按「匯入 Cookie 檔」。')
        elif operation == "open":
            self._log('請確認已安裝 Chrome 或 Edge，再按「開啟瀏覽器登入」重試；外部 Cookie 檔可另按「匯入 Cookie 檔」。')
        elif operation == "close":
            self._log('登入背景工作尚未清理完成；App 已保留，請再次按關閉重試。')
        self._update_controls()
        if self._close_pending:
            self._request_login_shutdown()

    @pyqtSlot(str)
    def _login_progress(self, message: str):
        self._login_progress_message = message
        self.append_status(message)
        self._refresh_summary()

    @pyqtSlot()
    def _login_thread_finished(self):
        self._login_thread = None
        self._login_worker = None
        self._login_busy = False
        self._login_browser_open = False
        self._login_operation = ""
        self._login_progress_message = ""
        self._update_controls()
        self._maybe_finish_close()

    def _auto_check_driver(self):
        if self.auto_driver_checkbox.isChecked():
            self.check_driver_update()

    def check_driver_update(self):
        if self._driver_thread is not None or self._close_pending:
            return
        thread = QThread(self)
        worker = DriverWorker(self.base_dir)
        worker.moveToThread(thread)
        self._driver_thread, self._driver_worker = thread, worker
        thread.started.connect(worker.run)
        worker.message.connect(self.append_status)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._driver_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._log('⏳ 正在背景檢查 ChromeDriver；新版下載可繼續使用。')
        self._update_controls()
        thread.start()

    @pyqtSlot()
    def _driver_thread_finished(self):
        self._driver_thread = None
        self._driver_worker = None
        self._update_controls()
        self._maybe_finish_close()

    def select_download_path(self):
        folder = QFileDialog.getExistingDirectory(self, tr("選擇下載資料夾"), self.download_path,
            options=QFileDialog.Option.DontUseNativeDialog)
        if folder:
            self.download_path = folder
            self.path_label.setText(folder)
            self._refresh_summary()
            self._log('📁 已選擇儲存路徑：{folder}', folder=folder)
            try:
                self._save_settings()
            except OSError as exc:
                self._log('⚠️ 無法保存設定：{exc}', exc=exc)

    def _load_settings(self):
        if not self.settings_file.is_file():
            return
        try:
            if self.settings_file.stat().st_size > 1024 * 1024:
                raise ValueError(tr('設定檔過大'))
            settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError(tr('設定檔格式不正確'))
            mode = settings.get("theme_mode", "light")
            self._usage_guide_seen = settings.get("usage_guide_seen") is True
            set_window_theme(self, mode if mode in ("light", "dark") else "light")
            for key, widget in (("hashtag", self.hashtag_input), ("limit_count", self.limit_count_input)):
                if isinstance(settings.get(key), str):
                    widget.setText(settings[key])
            if not isinstance(settings.get("hashtag"), str) and isinstance(settings.get("hashtags"), list):
                tags = parse_hashtags(settings["hashtags"])
                self.hashtag_input.setText(" ".join("#" + tag for tag in tags))
            if isinstance(settings.get("output_dir"), str):
                self.download_path = settings["output_dir"]
                self.path_label.setText(self.download_path)
            for key, widget in (("authors", self.authors_input), ("tweet_urls", self.tweet_urls_input)):
                if isinstance(settings.get(key), str):
                    widget.setPlainText(settings[key])
            for key, widget in self._checkbox_settings().items():
                if isinstance(settings.get(key), bool):
                    widget.setChecked(settings[key])
            proxy_port = settings.get("warp_proxy_port", 40000)
            if type(proxy_port) is int and 1 <= proxy_port <= 65535:
                self.warp_proxy_port_input.setValue(proxy_port)
            parsed_date = QDate.fromString(settings.get("since", ""), "yyyy-MM-dd")
            if parsed_date.isValid():
                self.limit_date_input.setDate(parsed_date)
        except (OSError, ValueError, TypeError) as exc:
            self._log('⚠️ 無法讀取設定，已使用預設值：{exc}', exc=exc)

    def _checkbox_settings(self):
        return {
            "search_enabled": self.search_checkbox, "author_media_enabled": self.author_media_checkbox,
            "discover_authors": self.discover_authors_checkbox, "retry_failed": self.retry_failed_checkbox,
            "folder_by_user": self.folder_by_user_checkbox, "delay": self.human_behavior_checkbox,
            "limit_count_enabled": self.limit_count_checkbox, "since_enabled": self.limit_date_checkbox,
            "auto_driver_update": self.auto_driver_checkbox,
            "warp_proxy": self.warp_proxy_checkbox,
            "include_author_replies": self.reply_media_checkbox,
        }

    def _save_settings(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "language": self.language_mode,
            "warp_proxy_port": self.warp_proxy_port_input.value(),
            "usage_guide_seen": self._usage_guide_seen,
            "theme_mode": self.theme_mode,
            "hashtag": self.hashtag_input.text(), "output_dir": self.download_path,
            "authors": self.authors_input.toPlainText(), "tweet_urls": self.tweet_urls_input.toPlainText(),
            "limit_count": self.limit_count_input.text(), "since": self.limit_date_input.date().toString("yyyy-MM-dd"),
        }
        try:
            settings["hashtags"] = list(parse_hashtags(self.hashtag_input.text()))
        except ValueError:
            # Preserve an unfinished edit on close; validate only on starting.
            settings["hashtags"] = []
        settings.update({key: widget.isChecked() for key, widget in self._checkbox_settings().items()})
        temporary = self.settings_file.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(settings, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _request_login_shutdown(self):
        if self._close_pending and self._login_thread is not None and not self._login_shutdown_requested:
            self._login_shutdown_requested = True
            self._login_busy = True
            self.browser_close_requested.emit()

    def _maybe_finish_close(self):
        if self._close_pending and self._download_thread is None and self._login_thread is None and self._driver_thread is None:
            self._allow_close = True
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent):
        background_active = self._download_thread is not None or self._login_thread is not None or self._driver_thread is not None
        if not self._allow_close and background_active:
            event.ignore()
            if not self._close_pending:
                self._close_pending = True
                self._log('⏳ 正在安全結束背景工作，完成後會自動關閉視窗。')
                self.stop_download()
                self._update_controls()
            self._request_login_shutdown()
            return
        try:
            self._save_settings()
        except OSError as exc:
            self._log('⚠️ 無法保存設定：{exc}', exc=exc)
        if self._usage_guide is not None:
            self._usage_guide.close()
        event.accept()


def main() -> int:
    smoke_test = "--smoke-test" in sys.argv
    if smoke_test:
        # Probe the independent helper without starting a job, reading cookies
        # or accessing X. The GUI process never imports gallery-dl.
        try:
            from gallery_client import GallerySource
            GallerySource.probe()
        except Exception as exc:
            if sys.stderr is not None:
                print(f"Smoke test failed: {exc}", file=sys.stderr)
            return 1
    app = QApplication([arg for arg in sys.argv if arg != "--smoke-test"])
    temporary = tempfile.TemporaryDirectory(prefix="fanart-smoke-") if smoke_test else None
    try:
        window = TwitterDownloader(
            show_notice=not smoke_test,
            base_dir=Path(temporary.name) if temporary is not None else None,
        )
        window.show()
        if smoke_test:
            def verify_owned_surfaces():
                failed = False
                try:
                    # Exercise the packaged native widgets too: the original
                    # regression affected surfaces outside the main layout.
                    window.advanced_toggle.setChecked(True)
                    window.limit_date_checkbox.setChecked(True)
                    assert not window.warp_proxy_checkbox.isChecked()
                    for language in ("zh_TW", "zh_CN", "en", "ja", "ko"):
                        window.language_combo.setCurrentIndex(window.language_combo.findData(language))
                        if language != "zh_TW":
                            assert tr("使用說明") != "使用說明", f"Missing {language} catalog"
                        if language != "en":
                            assert not app._fanart_qt_translator.isEmpty(), f"Missing {language} Qt translation"
                        for mode in ("light", "dark"):
                            set_window_theme(window, mode)
                            for dialog_type in (UserNoticeDialog, AuthorInfoDialog, UsageGuideDialog):
                                dialog = dialog_type(window, theme=mode)
                                dialog.show()
                                app.processEvents()
                                dialog.close()
                                dialog.deleteLater()
                                app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                            window.limit_date_input.show_calendar_popup()
                            app.processEvents()
                            window.limit_date_input.popup.hide()
                except Exception as exc:
                    failed = True
                    if sys.stderr is not None:
                        print(f"Native surface smoke failed: {exc}", file=sys.stderr)
                finally:
                    window.close()
                    if failed:
                        app.exit(1)
            QTimer.singleShot(0, verify_owned_surfaces)
        return app.exec()
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    sys.exit(main())
