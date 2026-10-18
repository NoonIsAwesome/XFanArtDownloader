"""Explicit local-login deletion confirmation and read-only update results."""
from html import escape
from urllib.parse import unquote, urlsplit

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from i18n import tr, locale as ui_locale
from ui_theme import apply_dialog_theme
from update_checker import PYPI_URL, RELEASES_URL, release_url


def confirm_clear_login(parent=None, theme="light") -> bool:
    # 刪除是使用者明確確認的獨立操作，預設取消；不把「登出」誤寫成撤銷網站工作階段。
    # Deletion needs explicit confirmation, defaults to Cancel, and does not revoke website sessions.
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(tr("清除本機登入資料"))
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setText(tr("確定清除本工具保存的登入資料？"))
    dialog.setInformativeText(tr(
        "將刪除 data/cookies.txt 與 data/browser-profile。專用設定檔可能也包含您在該視窗登入的 Google 狀態。\n"
        "請先關閉本工具的專用瀏覽器；日常 Chrome／Edge 不受影響。下載檔案、紀錄與設定會保留。\n"
        "這只移除本機副本，不會撤銷 X／Google 伺服器的登入；如需撤銷，請另到網站的帳號安全頁操作。"))
    dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    dialog.button(QMessageBox.StandardButton.Ok).setText(tr("清除登入資料"))
    dialog.button(QMessageBox.StandardButton.Cancel).setText(tr("取消"))
    dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
    dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
    apply_dialog_theme(dialog, theme)
    return dialog.exec() == QMessageBox.StandardButton.Ok


class UpdateResultDialog(QDialog):
    def __init__(self, result, parent=None, theme="light"):
        super().__init__(parent)
        self.theme_mode = theme if theme in {"light", "dark"} else "light"
        self._translated_labels = []
        self._component_labels = []
        self.setProperty("i18nSkip", True)
        self.setWindowTitle(tr("版本檢查"))
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = max(1, available.width() - 48) if available is not None else 760
        height = max(1, available.height() - 56) if available is not None else 700
        self.setMinimumSize(min(460, width), min(360, height))
        self.setMaximumSize(min(760, width), height)
        self.resize(min(580, width), min(600, height))
        self._allowed_urls = {RELEASES_URL, PYPI_URL}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(15)
        # 版本與連結可捲動，但關閉按鈕固定可見，避免長翻譯在小螢幕被裁掉。
        # Scroll results/links while keeping Close visible when translated copy is longer.
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)
        explanation = QLabel(tr("只檢查官方版本資訊，不會自動下載、安裝或替換程式。"))
        self._translated_labels.append((explanation, "只檢查官方版本資訊，不會自動下載、安裝或替換程式。"))
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        for key, title in (("app", "X FanArt Downloader"), ("gallery_dl", "gallery-dl")):
            component = result.get(key, {}) if isinstance(result, dict) else {}
            label = QLabel()
            self._component_labels.append((label, title, component))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
            links = [component.get("url"), *component.get("download_urls", [])[:3]]
            for url in dict.fromkeys(links):
                if not isinstance(url, str) or not (url in self._allowed_urls or release_url(url) or release_url(url, download=True)):
                    continue
                self._allowed_urls.add(url)
                source = "官方發布頁" if url == component.get("url") else "官方下載連結"
                suffix = "" if url == component.get("url") else " · " + unquote(urlsplit(url).path).rsplit("/", 1)[-1][:80]
                link = QLabel()
                self._translated_labels.append((link, source, url, suffix))
                link.setWordWrap(True)
                link.setOpenExternalLinks(False)
                link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
                link.setToolTip(url)
                link.linkActivated.connect(self._open_link)
                layout.addWidget(link)
        note = QLabel(tr("gallery-dl 更新需要重新建置 App；只更新原始碼環境不會改變現有 EXE。"))
        self._translated_labels.append((note, "gallery-dl 更新需要重新建置 App；只更新原始碼環境不會改變現有 EXE。"))
        note.setWordWrap(True)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch()
        button = QPushButton(tr("關閉"))
        self.close_button = button
        button.clicked.connect(self.accept)
        footer.addWidget(button)
        outer.addLayout(footer)
        self.retranslate_ui()
        self.set_theme(self.theme_mode)

    def retranslate_ui(self):
        # 保留既有結果與網址，只更換顯示文案；切換語言不重送任何查詢。
        # Reuse cached results and URLs; language changes never repeat a network check.
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("版本檢查"))
        self.close_button.setText(tr("關閉"))
        statuses = {
            "update_available": "有較新版本", "current": "已是目前發布版本", "ahead": "本機版本較新",
            "no_release": "尚無可用的正式 Release", "unavailable": "暫時無法取得官方資訊",
            "unknown": "無法比較版本",
        }
        for label, title, component in self._component_labels:
            installed = component.get("installed") or tr("無法確認")
            latest = component.get("latest") or tr("尚未取得")
            label.setText(f"{title}\n" + tr("本機：{installed}　官方：{latest}", installed=installed, latest=latest)
                          + "\n" + tr(statuses.get(component.get("status"), "無法比較版本")))
        for entry in self._translated_labels:
            label, source, *link = entry
            if link:
                url, suffix = link
                label.setText(f'<a href="{escape(url, quote=True)}">{escape(tr(source) + suffix)}</a>')
            else:
                label.setText(tr(source))

    refresh_language = retranslate_ui

    def set_theme(self, theme):
        self.theme_mode = theme if theme in {"light", "dark"} else "light"
        apply_dialog_theme(self, self.theme_mode)

    def _open_link(self, url):
        # 僅點擊才交給系統瀏覽器；確認網址仍在建構時驗證過的官方清單中。
        # Open only on explicit activation and only from the validated official-link allowlist.
        if url in self._allowed_urls:
            QDesktopServices.openUrl(QUrl(url))
