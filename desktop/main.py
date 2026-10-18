"""Gallery collection workbench backed by gallery-dl and durable history."""

# 主視窗只負責輸入、畫面與工作狀態；擷取、下載和登入交給各自的背景 worker。
# 如需新增來源，優先擴充下載引擎，避免把網站判斷或網路連線塞進介面事件。
# The window coordinates input and state; extend the engine for new sources and keep network work in workers.

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

from PyQt6.QtCore import QDate, QEvent, QObject, QThread, QTimer, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDateEdit, QFileDialog,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTextEdit, QWidget,
)

from author_dialog import AuthorInfoDialog
from app_version import APP_VERSION
from downloader import DownloadEngine, DownloadOptions, parse_hashtags
from login_browser import LoginBrowser, import_cookies
from login_browser import clear_login_data
from update_checker import UpdateCheckWorker
from update_dialog import UpdateResultDialog, confirm_clear_login
from resources_rc import get_icon_path
from ui_design import apply_dialog_theme, build_gallery_layout, refresh_gallery_summary, set_window_theme, WrappingCheckBox, theme_colors
from author_avatar import AvatarLoader, avatar_pixmap
from gallery_widgets import GalleryDateEdit, GallerySpinBox, UserNoticeDialog
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
    # 作者欄接受帳號或媒體頁；貼文連結屬於另一種來源，需交給下方網址欄。
    # Accept a handle or media page here; individual post links belong to the separate URL input.
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
    # 去除分享參數、圖片序號等外觀差異，讓同一貼文使用一致的去重鍵。
    # Normalize sharing parameters and photo indexes so each post has one deduplication key.
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
    # 背景執行引擎，僅以 signal 回報結果；這個執行緒不能直接修改 Qt 畫面。
    # Run the engine in the background and report through signals; never update widgets from this thread.
    message = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    progress = pyqtSignal(object)

    def __init__(self, options: DownloadOptions, stop_event: threading.Event):
        super().__init__()
        self.options = options
        self.stop_event = stop_event

    @pyqtSlot()
    def run(self):
        try:
            result = DownloadEngine(self.options, log=self.message.emit, stop_event=self.stop_event,
                                    progress=lambda event: self.progress.emit(dict(event))).run()
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class LoginWorker(QObject):
    # 開啟、匯出與關閉都沿用同一個登入 worker，避免跨執行緒操作瀏覽器程序。
    # Keep browser open, export and close operations on the same login worker thread.
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

    @pyqtSlot()
    def clear_login(self):
        # 只在明確確認後刪除本軟體的專用登入副本，不操作日常瀏覽器或網站上的工作階段。
        # Delete only this app's dedicated local login copy after confirmation, never the user's everyday browser.
        try:
            if self.browser is not None:
                self.browser.close()
            clear_login_data(self.profile_dir, self.cookie_file)
            self.browser = None
            self.completed.emit("clear", 0)
        except Exception as exc:
            self.failed.emit("clear", str(exc))


class DriverWorker(QObject):
    # ChromeDriver 為舊版相容功能；檢查失敗不應阻止新版 gallery-dl 下載。
    # ChromeDriver is a compatibility feature; its update failure must not block gallery-dl downloads.
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
    # 此控制器集中管理三類背景工作；排版與外觀則放在 ui_design.py。
    # This controller owns background job state; ui_design.py owns layout and visual presentation.
    browser_open_requested = pyqtSignal()
    cookie_save_requested = pyqtSignal()
    browser_close_requested = pyqtSignal()
    login_clear_requested = pyqtSignal()

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
        # 執行狀態和設定值分開保存；重新開始時會建立全新的停止旗標與 worker。
        # Keep runtime state separate from preferences; each new run gets a fresh worker and stop event.
        self.download_path = str(self.base_dir / "downloads")
        self.successful_downloads = 0
        self._active_retry_count = 0
        self._active_redownload_count = 0
        self._active_redownload_mode = "copy"
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
        self._update_thread = None
        self._update_worker = None
        self._update_dialog = None
        self._history_dialog = None
        self._close_pending = False
        self._allow_close = False
        self._usage_guide = None
        self._usage_guide_seen = False
        self._status_entries = []
        self._result_detail = None
        self._current_media = None
        self._avatar_image = None

        self.setWindowIcon(QIcon(get_icon_path()))
        self.setWindowTitle(f"FanArt Downloader {APP_VERSION}")

        # 基本輸入與下載紀錄：新安裝不預填任何作者專用 FanArt 標籤。
        # Create the basic inputs and log without pre-filling any personal FanArt tag.
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
        self.status_box = QPlainTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setAccessibleName(tr('下載狀態與錯誤訊息'))
        self.status_box.setMinimumHeight(100)
        self.status_box.document().setMaximumBlockCount(2000)

        # 三種收集入口可搭配使用：標籤搜尋、作者補抓、使用者貼上的貼文網址。
        # Tag search, author backfill and pasted post URLs can be combined in one collection.
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
        # 進階選項只影響本次任務；WARP 預設關閉，不會修改電腦的 DNS 或 VPN。
        # Advanced options apply to this job; WARP starts disabled and never changes system DNS or VPN settings.
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
        self.warp_proxy_port_input = GallerySpinBox()
        self.warp_proxy_port_input.setRange(1, 65535)
        self.warp_proxy_port_input.setValue(40000)
        self.warp_proxy_port_input.setAccessibleName(tr("本機代理連接埠"))

        self.limit_count_checkbox = WrappingCheckBox(tr("搜尋貼文上限"))
        self.limit_count_input = QLineEdit("100")
        self.limit_count_input.setAccessibleName(tr("標籤搜尋的貼文上限"))
        self.limit_date_checkbox = WrappingCheckBox(tr('只處理此日期起的貼文'))
        self.limit_date_input = GalleryDateEdit(theme=self.theme_mode)
        self.limit_date_input.setDisplayFormat("yyyy-MM-dd")
        self.limit_date_input.setCalendarPopup(True)
        self.limit_date_input.setDate(QDate.currentDate())
        self.limit_date_input.setAccessibleName(tr('貼文起始日期'))
        self.date_window_search_checkbox = WrappingCheckBox(tr("依日期分段搜尋，減少漏抓"))
        self.date_window_search_checkbox.setChecked(True)
        self.date_window_search_checkbox.setToolTip(tr("需設定起始日期；分段查詢仍遵守搜尋上限與 X 回傳的可見範圍。"))
        self.resume_checkbox = WrappingCheckBox(tr("接續未完成的收集工作"))
        self.resume_checkbox.setChecked(True)
        self.resume_checkbox.setToolTip(tr("相同設定會接續未完成來源；該段可能重新掃描，已下載的媒體不會重複下載。"))
        self.human_behavior_checkbox = WrappingCheckBox(tr('下載間隨機休息（假裝自己不是機器人）'))
        self.human_behavior_checkbox.setChecked(True)

        # 先建立畫面再載入偏好；非同步頭像完成後才由主執行緒更新元件。
        # Build widgets before restoring preferences; deliver asynchronous avatars to the GUI thread.
        self.setup_ui()
        self._avatar_loader = AvatarLoader(self)
        self._avatar_loader.ready.connect(self._avatar_ready)
        self.setup_signals()
        self._load_settings()
        self._update_controls()
        self.refresh_language()
        if show_notice:
            # 說明只由使用者點按開啟，啟動時不打斷收集工作。
            # Open help only on explicit activation; startup never interrupts the collection workflow.
            QTimer.singleShot(0, self._auto_check_driver)

    def setup_ui(self):
        build_gallery_layout(self)

    def setup_signals(self):
        # 將操作集中接線，避免切換語言或主題時重複連接並觸發多次下載。
        # Connect actions once so language or theme changes cannot trigger duplicate downloads.
        self.cookie_btn.clicked.connect(self.open_browser)
        self.cookie_save_btn.clicked.connect(self.save_cookies)
        self.cookie_import_btn.clicked.connect(self.import_cookie_file)
        self.choose_path_btn.clicked.connect(self.select_download_path)
        self.open_download_folder_btn.clicked.connect(self.open_download_folder)
        self.download_btn.clicked.connect(self.start_download_thread)
        self.stop_btn.clicked.connect(self.stop_download)
        self.driver_check_btn.clicked.connect(self.check_driver_update)
        self.author_media_checkbox.toggled.connect(self._update_controls)
        self.limit_count_checkbox.toggled.connect(self._update_controls)
        self.limit_date_checkbox.toggled.connect(self._update_controls)
        self.warp_proxy_checkbox.toggled.connect(self._refresh_history_route)
        self.warp_proxy_checkbox.toggled.connect(self._update_controls)
        self.warp_proxy_port_input.valueChanged.connect(self._refresh_history_route)
        self.hashtag_input.textChanged.connect(self._refresh_summary)
        self.search_checkbox.toggled.connect(self._refresh_summary)
        self.discover_authors_checkbox.toggled.connect(self._refresh_summary)
        self.retry_failed_checkbox.toggled.connect(self._refresh_summary)
        self.reply_media_checkbox.toggled.connect(self._refresh_summary)
        self.authors_input.textChanged.connect(self._refresh_summary)
        self.tweet_urls_input.textChanged.connect(self._refresh_summary)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.about_action.triggered.connect(self.show_author_info)
        self.help_action.triggered.connect(self.show_usage_guide)
        self.language_combo.currentIndexChanged.connect(self._language_selected)
        self.history_action.triggered.connect(self.show_download_history)
        self.history_panel.retryRequested.connect(self._retry_history_items)
        self.history_panel.redownloadRequested.connect(self._redownload_history_items)
        self.update_action.triggered.connect(self.check_updates)
        self.clear_login_action.triggered.connect(self.clear_saved_login)

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
        # 在原有元件上更新文字，保留輸入、工作連線與下載進度，不重建視窗。
        # Translate existing widgets in place, preserving inputs, worker connections and current progress.
        install_qt_translator(QApplication.instance())
        self.setLocale(ui_locale())
        translate_widget_tree(self)
        self.language_combo.blockSignals(True)
        self.language_combo.setItemText(0, tr("跟隨電腦設定"))
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language_mode)))
        self.language_combo.blockSignals(False)
        self.language_combo.setAccessibleName(tr("介面語言"))
        self.language_combo.setToolTip(tr("選擇介面語言；系統選項會跟隨電腦設定。"))
        self.main_tabs.setTabText(0, tr("下載設定"))
        self.main_tabs.setTabText(1, tr("下載狀況與預覽"))
        # Theme-dependent labels are generated afresh from canonical text.
        set_window_theme(self, self.theme_mode)
        self._refresh_summary()
        if self._result_detail is not None:
            template, values = self._result_detail
            self.result_detail_label.setText(tr(template, **values))
        self.history_panel.set_session_log(self._status_entries)
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
        # 保存「訊息範本＋參數」供切換語言時重譯；檔名與作者帳號保持原樣。
        # Store message templates and arguments for retranslation while keeping filenames and handles literal.
        self._record_status(template, values)

    def _record_status(self, template, values):
        # 完整原始範本持續保留，不依紀錄視窗是否開啟；兩個檢視皆有固定容量。
        # Retain canonical templates even before history opens; both views have a fixed capacity.
        entry = (template, values)
        self._status_entries.append(entry)
        del self._status_entries[:-2000]
        if self._history_dialog is not None:
            self._history_dialog.append_session_log(entry)
        else:
            self._append_status_text(self._render_status(*entry))
        # 設定頁也保留短回饋，錯誤或登入提示不會只留在尚未開啟的日誌中。
        # Keep short global feedback so errors/login prompts never disappear into an unseen activity log.
        if template.startswith(("⚠", "❌", "🔓")):
            self._set_result_detail(template, **(values or {}))

    @staticmethod
    def _public_text(value, maximum=200):
        # 作者名稱／檔名屬於外部資料；限制長度並移除控制字元後才放進畫面。
        # Treat external names and filenames as bounded plain text and strip control characters.
        if not isinstance(value, str):
            return ""
        return "".join(char for char in value[:maximum] if not unicodedata.category(char).startswith("C")).strip()

    @pyqtSlot(object)
    def _avatar_ready(self, image):
        if not self._close_pending and not self._stop_event.is_set():
            self._avatar_image = image
            self.refresh_current_media()

    @pyqtSlot()
    def _reveal_download_status(self):
        # 作者列顯示後等版面更新，再讓目前作者與執行紀錄同時可見。
        # Wait for the author row's layout before revealing the current author and log together.
        if self._close_pending or self._stop_event.is_set() or self._current_media is None:
            return
        # 進度不能搶走正在編輯的設定頁；日誌在狀況頁自己的區域內捲動。
        # Progress never steals the settings page; the activity log scrolls only inside its own area.
        if self.main_tabs.currentIndex() == 1:
            QTimer.singleShot(0, self._scroll_download_status)

    @pyqtSlot()
    def _scroll_download_status(self):
        if self._close_pending or self._stop_event.is_set():
            return
        # QTextEdit 的焦點代理只會捲到游標；紀錄位於側欄最底端，直接捲到末端。
        # QTextEdit's focus proxy reveals only its cursor; the log is last, so scroll to the end.
        if self.main_tabs.currentIndex() != 1:
            return
        bar = self.status_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "sidebar_scroll") and self._current_media is not None:
            QTimer.singleShot(0, self._reveal_download_status)

    def _media_progress(self, worker, event):
        # 比對 worker 身分，而非只看是否下載中：上一輪排隊中的 signal 可能較晚送達。
        # Check worker identity because queued signals from an earlier run may arrive after a restart.
        # 已停止或前輪排隊中的事件不可重新啟動頭像請求，或取代新工作的作者資訊。
        # Queued events from stopped/previous runs cannot restart avatar requests or replace the new author.
        if (worker is not self._download_worker or not self.is_downloading
                or self._stop_event.is_set() or self._close_pending or not isinstance(event, dict)
                or event.get("type") != "media" or event.get("state") not in {"downloading", "downloaded", "skipped"}):
            return
        previous = self._current_media
        self._current_media = {key: self._public_text(event.get(key), maximum)
                               for key, maximum in (("author_handle", 15), ("author_name", 100),
                                                    ("filename", 160), ("display_path", 200))}
        self._current_media["state"] = event["state"]
        if previous is None or previous.get("author_handle") != self._current_media["author_handle"]:
            self._avatar_image = None
        self.refresh_current_media()
        self._avatar_loader.load(event.get("avatar_url"))
        if event["state"] in {"downloaded", "skipped"}:
            self.history_panel.schedule_refresh()
        QTimer.singleShot(0, self._reveal_download_status)

    def _refresh_history_route(self, *_ignored):
        # 紀錄視窗可保持開啟；主窗變更代理後，先中止舊路由再處理目前選取。
        # A nonmodal history window follows live route changes, cancelling its old connection first.
        if self._history_dialog is not None and not self._close_pending:
            self._history_dialog.configure_route(self.warp_proxy_checkbox.isChecked(), self.warp_proxy_port_input.value())

    def refresh_current_media(self):
        # 主頁只呈現公開作者與檔名；本機圖片預覽集中在下載紀錄視窗。
        # Keep the main page to the public author and filename; local images belong to download history.
        if not hasattr(self, "current_media_row"):
            return
        colors = theme_colors(self.theme_mode)
        self.current_media_row.setVisible(self._current_media is not None)
        self.current_avatar_label.setPixmap(avatar_pixmap(self._avatar_image, colors))
        self.current_avatar_label.setToolTip(tr("作者公開頭像") if self._avatar_image is not None
                                             else tr("未提供頭像，或頭像暫時無法載入。"))
        if self._current_media is not None:
            data = self._current_media
            handle = "@" + data["author_handle"] if data["author_handle"] else ""
            name = data["author_name"]
            title = " · ".join(value for value in (name, handle) if value)
            self.current_author_label.set_literal(title or tr("作者資料未提供"))
            self.current_file_label.set_literal(data["display_path"] or data["filename"])
        if self._current_media is None:
            self.status_box.setMaximumHeight(16777215)

    def toggle_theme(self):
        set_window_theme(self, "dark" if self.theme_mode == "light" else "light")
        if self._usage_guide is not None:
            self._usage_guide.set_theme(self.theme_mode)
        for dialog in (self._history_dialog, self._update_dialog):
            if dialog is not None:
                dialog.set_theme(self.theme_mode)
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
            login_status = self._login_progress_message or {
                "open": "正在開啟專用登入視窗…", "clear": "正在清除本機登入資料…",
            }.get(self._login_operation, "正在讀取並儲存 X 登入狀態…")
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
        self._record_status(str(message), None)

    def _append_status_text(self, message):
        # Post text and filenames may contain markup; always display plain text.
        cursor = self.status_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(str(message) + "\n")
        self.status_box.setTextCursor(cursor)
        self.status_box.ensureCursorVisible()

    def _update_controls(self, *_):
        # 收集或安全關閉期間鎖定會改變任務內容的欄位，讓設定快照保持一致。
        # Lock job-changing inputs during collection or shutdown to preserve the settings snapshot.
        editable = not self.is_downloading and not self._close_pending
        for widget in (
            self.hashtag_input, self.choose_path_btn, self.search_checkbox,
            self.author_media_checkbox, self.tweet_urls_input, self.retry_failed_checkbox,
            self.folder_by_user_checkbox, self.human_behavior_checkbox,
            self.reply_media_checkbox,
            self.limit_count_checkbox, self.limit_date_checkbox,
            self.warp_proxy_checkbox,
            self.date_window_search_checkbox, self.resume_checkbox,
        ):
            widget.setEnabled(editable)
        author_enabled = editable and self.author_media_checkbox.isChecked()
        self.authors_input.setEnabled(author_enabled)
        self.discover_authors_checkbox.setEnabled(author_enabled)
        self.limit_count_input.setEnabled(editable and self.limit_count_checkbox.isChecked())
        self.limit_date_input.setEnabled(editable and self.limit_date_checkbox.isChecked())
        self.date_window_search_checkbox.setEnabled(editable and self.limit_date_checkbox.isChecked() and self.search_checkbox.isChecked())
        self.warp_proxy_port_input.setEnabled(editable and self.warp_proxy_checkbox.isChecked())
        self.download_btn.setEnabled(editable and not self._login_busy)
        self.cookie_btn.setEnabled(editable and not self._login_busy)
        self.cookie_save_btn.setEnabled(editable and not self._login_busy)
        self.cookie_import_btn.setEnabled(editable and not self._login_busy)
        self.stop_btn.setEnabled(self.is_downloading and not self._stop_event.is_set())
        self.auto_driver_checkbox.setEnabled(not self._close_pending)
        self.driver_check_btn.setEnabled(self._driver_thread is None and not self._close_pending)
        self.language_combo.setEnabled(not self._close_pending)
        self.history_action.setEnabled(not self._close_pending)
        redownload_active = self.is_downloading and bool(self._active_redownload_count)
        self.open_download_folder_btn.setEnabled(not self._close_pending and not redownload_active)
        self.open_download_folder_btn.setToolTip(tr("重新下載使用各項目原儲存資料夾；可從下載紀錄開啟。")
            if redownload_active else tr("開啟下載資料夾"))
        self.update_action.setEnabled(self._update_thread is None and not self._close_pending)
        self.clear_login_action.setEnabled(editable and not self._login_busy)
        if self._history_dialog is not None:
            self._history_dialog.set_retry_enabled(editable and not self._login_busy)
        self._refresh_summary()

    def snapshot_options(self) -> DownloadOptions:
        """Read widgets once on the GUI thread; workers only receive this value."""
        # 先完整驗證所有欄位，再交出一次性的設定快照；失敗時不啟動背景工作。
        # Validate every input before creating the immutable job snapshot or starting background work.
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
                raise ValueError(tr("搜尋貼文上限必須是大於 0 的整數。")) from exc
            if limit_count <= 0:
                raise ValueError(tr("搜尋貼文上限必須是大於 0 的整數。"))
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
            date_window_search=self.date_window_search_checkbox.isChecked(),
            resume=self.resume_checkbox.isChecked(),
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
            self._log('無法記住說明視窗偏好；下次仍可從「更多功能 → 使用說明」開啟。')

    def start_download_thread(self):
        # 舊執行緒真正結束後才允許重開，避免停止中的任務與新任務共用資源。
        # Wait for the old thread to finish before allowing a new run to share its resources.
        if self.is_downloading or self._login_busy or self._close_pending or self._download_thread is not None:
            return
        try:
            options = self.snapshot_options()
        except (ValueError, OSError) as exc:
            self._validation_error(str(exc))
            return
        self._start_download_options(options)

    def _start_download_options(self, options):
        # 收集、失敗重試與重新下載共用工作生命週期；重新下載的檔案規則仍交給引擎。
        # Collection, retries and redownloads share one lifecycle; the engine owns file commit rules.
        if self.is_downloading or self._login_busy or self._close_pending or self._download_thread is not None:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._save_settings()
        except OSError as exc:
            self._validation_error(str(exc))
            return
        self.successful_downloads = 0
        self._active_retry_count = len(options.retry_media_ids)
        self._active_redownload_count = len(options.redownload_media_ids)
        self._active_redownload_mode = options.redownload_mode
        self._avatar_loader.cancel()
        self._avatar_loader.configure(options.warp_proxy, options.warp_proxy_port)
        self.history_panel.configure_route(options.warp_proxy, options.warp_proxy_port)
        self._current_media = self._avatar_image = None
        self.refresh_current_media()
        self.is_downloading = True
        self._run_state = "正在收集"
        if options.redownload_media_ids:
            self._set_result_detail("正在重新下載選取媒體；原檔會保留到新檔下載並驗證成功。")
        elif options.retry_media_ids:
            self._set_result_detail("正在重試選取媒體；只使用紀錄中已驗證的收集範圍。")
        else:
            self._set_result_detail('正在逐一核對來源與完整標籤；下載紀錄會持續更新。')
        # 每輪獨立的取消旗標可防止「停止後重開」立即被上一輪的停止狀態取消。
        # A fresh stop event prevents a restarted run from inheriting the previous cancellation.
        self._stop_event = threading.Event()
        thread = QThread(self)
        worker = DownloadWorker(options, self._stop_event)
        worker.moveToThread(thread)
        self._download_thread = thread
        self._download_worker = worker
        thread.started.connect(worker.run)
        worker.message.connect(self.append_status)
        worker.progress.connect(lambda event, owner=worker: self._media_progress(owner, event))
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._download_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._update_controls()
        targets = "、".join("#" + tag for tag in options.hashtags)
        if options.redownload_media_ids:
            mode = tr("覆寫原檔") if options.redownload_mode == "overwrite" else tr("另存副本")
            self._log("⏳ 正在重新下載選取的 {count} 個媒體（{mode}）；只處理原儲存位置的選取項目。",
                      count=len(options.redownload_media_ids), mode=mode)
        elif options.retry_media_ids:
            self._log("⏳ 正在重試選取的 {count} 個媒體；其餘來源不會啟動。", count=len(options.retry_media_ids))
        else:
            self._log("⏳ 開始處理 {targets}；原貼文須符合任一完整標籤，同串原作者留言依補抓設定處理，重複媒體只下載一次。", targets=targets)
        thread.start()
        self.main_tabs.setCurrentIndex(1)

    @pyqtSlot(object)
    def _download_completed(self, result):
        # 成功、部分完成與使用者停止分開呈現；有來源錯誤時不能顯示全部完成。
        # Distinguish success, partial completion and cancellation; source errors must not look like full success.
        incomplete = getattr(result, "incomplete_sources", [])
        if result.stopped:
            self._avatar_loader.cancel()
        self.successful_downloads = result.downloaded
        if result.stopped:
            state = "🛑 已停止"
        elif result.errors or result.failed or incomplete or getattr(result, "job_state", "") in {"partial", "stopped"}:
            state = "⚠️ 部分完成／有失敗項目" if result.downloaded or result.skipped or result.posts else "❌ 未完成"
        else:
            state = "✅ 本輪處理完成"
        self._run_state = state.split(" ", 1)[-1]
        self._set_result_detail('✅ 下載 {downloaded} · ⏭️ 略過 {skipped} · ❌ 失敗 {failed}', downloaded=result.downloaded, skipped=result.skipped, failed=result.failed)
        self._refresh_summary()
        self._log('{state}：下載 {downloaded} 個媒體，略過 {skipped}，失敗 {failed}，標籤／日期不符 {filtered}；處理 {posts} 則貼文。', state=state, downloaded=result.downloaded, skipped=result.skipped, failed=result.failed, filtered=result.filtered, posts=result.posts)
        # 來源完整性與媒體下載結果分開報告，避免「檔案成功」掩蓋尚未查完的搜尋。
        # Report source coverage separately so successful files cannot hide unfinished collection sources.
        if getattr(result, "resumed", False):
            self._log("↪️ 已接續先前未完成的工作；重新掃描的段落會略過已下載媒體。")
        if getattr(result, "job_id", None) is not None:
            self._log("📋 來源進度：累計完成 {completed}、本輪沿用 {skipped}、未完成 {pending}。",
                      completed=getattr(result, "sources_completed", 0), skipped=getattr(result, "sources_skipped", 0), pending=len(incomplete))
        if getattr(result, "pending_media", 0):
            self._log("📋 仍有 {count} 個媒體待處理，可在下載紀錄查看。", count=result.pending_media)
        if incomplete:
            self._log("📋 仍有 {count} 個來源未完成；可在「更多功能 → 下載紀錄與收集進度」查看。", count=len(incomplete))
        elif result.stopped:
            self._log("📋 本次已停止；已下載媒體與可用進度會保留。")
        elif not result.errors:
            self._log("📋 本次排定來源已處理完畢；X 未回傳的內容仍可能無法收集。")
        if self._history_dialog is not None:
            self._history_dialog.refresh(preserve_preview=True)
        for error in result.errors[:5]:
            self._log("⚠️ {message}", message=str(error))
        if len(result.errors) > 5:
            self._log('另有 {count} 筆錯誤，請參考上方紀錄。', count=len(result.errors) - 5)

    @pyqtSlot(str)
    def _download_failed(self, message: str):
        self._avatar_loader.cancel()
        self._run_state = "下載未完成"
        self._set_result_detail('請在「下載狀況與預覽」查看紀錄，修正問題後可重新開始。')
        self._refresh_summary()
        self._log("❌ 下載未完成：{message}", message=message)

    @pyqtSlot()
    def _download_thread_finished(self):
        # 完成訊息可能早於 QThread 釋放；在 finished 才解鎖下一次開始按鈕。
        # A result can arrive before QThread teardown; unlock the next run only after finished.
        self.is_downloading = False
        self._download_thread = None
        self._download_worker = None
        self._update_controls()
        self._maybe_finish_close()

    def stop_download(self):
        # 採合作式取消，讓引擎收尾暫存檔與資料庫交易，不強制終止寫入中的執行緒。
        # Cancel cooperatively so the engine can finish temporary-file and database cleanup safely.
        if self.is_downloading and not self._stop_event.is_set():
            self._stop_event.set()
            self._avatar_loader.cancel()
            self._run_state = "正在安全停止…"
            self._log('🛑 正在停止；等待目前的連線結束後安全保存下載紀錄。')
            self._update_controls()

    def _ensure_login_worker(self):
        # 登入 worker 可重用；只在需要時建立，並將三種瀏覽器操作排到同一執行緒。
        # Reuse one login worker and queue all browser operations onto its owning thread.
        if self._login_thread is not None:
            return
        thread = QThread(self)
        worker = LoginWorker(self.data_dir / "browser-profile", self.cookie_file)
        worker.moveToThread(thread)
        self._login_thread, self._login_worker = thread, worker
        self.browser_open_requested.connect(worker.open_browser)
        self.cookie_save_requested.connect(worker.save_cookies)
        self.browser_close_requested.connect(worker.close_browser)
        self.login_clear_requested.connect(worker.clear_login)
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
        # 匯入流程交由登入模組驗證與限制 Cookie 網域；介面不讀出或顯示內容。
        # Let the login module validate cookie domains; never display or inspect their values in the UI.
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

    def clear_saved_login(self):
        # 刪除登入資料必須是使用者主動選擇並確認，不與關閉視窗或一般下載連動。
        # Clear login only after an explicit user action and confirmation, never as part of closing or downloading.
        if self.is_downloading or self._login_busy or self._close_pending:
            return
        if not confirm_clear_login(parent=self, theme=self.theme_mode):
            return
        self._ensure_login_worker()
        self._login_busy = True
        self._login_operation = "clear"
        self._login_progress_message = ""
        self._update_controls()
        self._log("⏳ 正在清除本工具的本機登入資料；下載檔案與紀錄會保留。")
        self.login_clear_requested.emit()

    def show_download_history(self):
        # 入口只切換主視窗分頁，不另開紀錄視窗或建立第二份狀態。
        # Navigate to the main activity page without creating another window or duplicate state.
        if self._close_pending:
            return
        self.history_panel.configure_route(self.warp_proxy_checkbox.isChecked(), self.warp_proxy_port_input.value())
        self.history_panel.set_retry_enabled(not self.is_downloading and not self._login_busy)
        if self.main_tabs.currentIndex() == 1:
            self.history_panel.refresh(preserve_preview=True)
        else:
            self.main_tabs.setCurrentIndex(1)

    @pyqtSlot(object)
    def _retry_history_items(self, media_ids):
        if self.is_downloading or self._login_busy or self._close_pending or self._download_thread is not None:
            return
        if not isinstance(media_ids, (tuple, list)) or not media_ids or any(not isinstance(item, str) for item in media_ids):
            return
        # 選取重試以資料庫中已驗證的原始標籤／討論串為範圍，不要求重新填寫目前表單。
        # Selected retries use the record's validated tag/thread scope without requiring the current form to be filled again.
        options = DownloadOptions(
            hashtag="", output_dir=Path(self.download_path).expanduser(), database_path=self.data_dir / "downloads.sqlite3",
            cookie_file=self.cookie_file if self.cookie_file.is_file() else None,
            retry_media_ids=tuple(dict.fromkeys(media_ids)), search_enabled=False, discover_authors=False,
            authors=(), tweet_urls=(), since=None, resume=False,
            folder_by_user=self.folder_by_user_checkbox.isChecked(), delay=self.human_behavior_checkbox.isChecked(),
            warp_proxy=self.warp_proxy_checkbox.isChecked(), warp_proxy_port=self.warp_proxy_port_input.value(),
        )
        self._start_download_options(options)

    @pyqtSlot(object, str)
    def _redownload_history_items(self, media_ids, mode):
        # 選擇與覆寫確認由紀錄視窗完成；此處再次限制模式與工作範圍，不採用目前搜尋表單。
        # History handles the choice and overwrite confirmation; recheck scope without using the search form.
        if self.is_downloading or self._login_busy or self._close_pending or self._download_thread is not None:
            return
        if not isinstance(mode, str) or mode not in {"copy", "overwrite"}:
            return
        if not isinstance(media_ids, (tuple, list)) or not media_ids or any(not isinstance(item, str) or not item for item in media_ids):
            return
        options = DownloadOptions(
            hashtag="", output_dir=Path(self.download_path).expanduser(), database_path=self.data_dir / "downloads.sqlite3",
            cookie_file=self.cookie_file if self.cookie_file.is_file() else None,
            redownload_media_ids=tuple(dict.fromkeys(media_ids)), redownload_mode=mode,
            retry_failed=False, search_enabled=False, discover_authors=False,
            authors=(), tweet_urls=(), since=None, resume=False,
            folder_by_user=self.folder_by_user_checkbox.isChecked(), delay=self.human_behavior_checkbox.isChecked(),
            warp_proxy=self.warp_proxy_checkbox.isChecked(), warp_proxy_port=self.warp_proxy_port_input.value(),
        )
        self._start_download_options(options)

    def check_updates(self):
        # 官方版本查詢也放在背景；不讀登入資料、不下載更新檔、不更動執行中的 EXE。
        # Check official metadata in the background without login data, update downloads or replacing the running EXE.
        if self._update_thread is not None or self._close_pending:
            return
        thread = QThread(self)
        worker = UpdateCheckWorker()
        worker.moveToThread(thread)
        self._update_thread, self._update_worker = thread, worker
        thread.started.connect(worker.run)
        worker.completed.connect(self._updates_ready)
        worker.failed.connect(self._update_check_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._update_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._log("⏳ 正在檢查 App 與 gallery-dl 的官方版本資訊…")
        self._update_controls()
        thread.start()

    @pyqtSlot(object)
    def _updates_ready(self, result):
        if self._close_pending:
            return
        if self._update_dialog is not None:
            self._update_dialog.close()
            self._update_dialog.deleteLater()
        self._update_dialog = UpdateResultDialog(result, parent=self, theme=self.theme_mode)
        self._update_dialog.show()
        self._log("✅ 版本檢查結束，請查看官方版本資訊。")

    @pyqtSlot(str)
    def _update_check_failed(self, message):
        # 版本查詢失敗也要進入跨分頁的短提示，不能只留在背景日誌。
        # Surface update-check failures in global feedback, not only in the background log.
        if not self._close_pending:
            self._log("⚠️ {message}", message=message)

    @pyqtSlot()
    def _update_thread_finished(self):
        self._update_thread = self._update_worker = None
        self._update_controls()
        self._maybe_finish_close()

    @pyqtSlot(str, int)
    def _login_completed(self, operation: str, count: int):
        self._login_busy = False
        self._login_operation = ""
        self._login_progress_message = ""
        if operation == "open":
            self._login_browser_open = True
            self._login_status_message = ""
            self._log('🔓 請在專用瀏覽器中完成 X 或 Google 登入。登入完成後，請關閉剛才的專用瀏覽器視窗，再回來按儲存。')
        elif operation == "clear":
            self._login_browser_open = False
            self._login_status_message = "本機登入資料已清除"
            self._log("✅ 已清除本工具的 Cookie 與專用瀏覽器資料；下載檔案、紀錄與設定已保留。")
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
            "clear": "清除登入資料未完成，請查看紀錄。",
        }.get(operation, "登入操作未完成，請查看紀錄。")
        if operation == "open":
            self._login_browser_open = False
        elif operation == "close":
            # Retain the worker and its browser object so another close attempt
            # can finish cleaning up the exporter it owns.
            self._login_shutdown_requested = False
            self._close_pending = False
        self._log('❌ 登入瀏覽器{operation}失敗：{message}', operation={"save": "儲存", "clear": "清除"}.get(operation, "操作"), message=message)
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

    def open_download_folder(self):
        # 明確點按才建立並開啟目前選擇的資料夾；交由 Qt 傳遞本機 URL，不組合 shell 指令。
        # Create/open the selected folder only on explicit activation, using a local Qt URL instead of shell commands.
        if self._close_pending or (self.is_downloading and self._active_redownload_count):
            return
        try:
            folder = Path(self.download_path).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            folder = folder.resolve(strict=True)
            if not folder.is_dir() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError("Folder could not be opened")
        except (OSError, ValueError, RuntimeError):
            self._log("⚠️ 無法開啟下載資料夾，請確認路徑與存取權限。")

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
        # 舊版／損壞設定採型別與大小檢查；缺少的欄位保留安全預設值。
        # Validate settings size and types; keep safe defaults for missing or malformed legacy fields.
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
            "date_window_search": self.date_window_search_checkbox,
            "resume": self.resume_checkbox,
        }

    def _save_settings(self):
        # 這裡只存操作偏好，登入憑證另由登入模組管理，不混入一般設定 JSON。
        # Store preferences here; the login module manages credentials separately from settings JSON.
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
        # 先寫暫存檔並落盤，再原子取代；寫入中斷時仍保有上一次完整設定。
        # Flush a temporary file before atomic replacement so interrupted writes preserve the last complete settings.
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
        # 等下載、登入與驅動檢查都完成收尾，再重新送出關閉事件。
        # Retry closing only after download, login and driver workers have finished their cleanup.
        if self._close_pending and self._download_thread is None and self._login_thread is None and self._driver_thread is None and self._update_thread is None:
            self._allow_close = True
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent):
        # 關閉分兩階段：先提出停止請求，背景工作結束後才保存偏好與釋放視窗。
        # Close in two stages: request cancellation, then save preferences and release widgets after workers stop.
        self._avatar_loader.cancel()
        background_active = self._download_thread is not None or self._login_thread is not None or self._driver_thread is not None or self._update_thread is not None
        if not self._allow_close and background_active:
            event.ignore()
            if not self._close_pending:
                self._close_pending = True
                self._log('⏳ 正在安全結束背景工作，完成後會自動關閉視窗。')
                self.stop_download()
                if self._update_worker is not None:
                    self._update_worker.cancel()
                self._update_controls()
            self._request_login_shutdown()
            return
        try:
            self._save_settings()
        except OSError as exc:
            self._log('⚠️ 無法保存設定：{exc}', exc=exc)
        if self._usage_guide is not None:
            self._usage_guide.close()
        for dialog in (self._history_dialog, self._update_dialog):
            if dialog is not None:
                dialog.close()
        self._avatar_loader.close()
        event.accept()


def main() -> int:
    # smoke-test 使用獨立暫存資料；測試啟動、翻譯與視窗收尾時不碰真實帳號。
    # Smoke tests use isolated temporary data and never read the real account during startup or translation checks.
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
        exit_code = app.exec()
        # 事件迴圈已結束，背景工作也已由 closeEvent 收尾。趁 Python／SIP
        # 仍完整運作時釋放主視窗與其子元件，不把原生 Qt 清理留到直譯器結束。
        # After the event loop and worker cleanup finish, release native widgets while Python and SIP are still fully alive.
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return exit_code
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    sys.exit(main())
