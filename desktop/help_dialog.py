"""A local, themed reading guide. Opening it never logs in or makes a request."""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ui_design import line_icon
from ui_theme import apply_dialog_theme, theme_colors
from i18n import tr, locale as ui_locale


STEPS = (
    (
        "填入想收集的標籤",
        "請自行填入完整 #標籤，多個標籤可用空白、逗號或分號分隔。"
        "符合任一標籤即可（OR）；搜尋、作者媒體頁與貼文網址的原貼文都使用相同的完整標籤條件。",
    ),
    (
        "一般瀏覽器登入，關窗後再儲存",
        "按「開啟瀏覽器登入」，在本工具專用的 Chrome／Edge 視窗完成 X 登入；"
        "使用 Google 登入時，也在這個一般瀏覽器內操作。",
    ),
    (
        "自動作者補抓，手動填寫可留空",
        "自動：使用本輪與歷史紀錄已發現的作者，不必重填作者欄。\n"
        "手動：補上尚未發現的畫師，可填 @帳號 或作者的 /media 網址，不用數字 ID；"
        "已知貼文也可直接貼網址。作者媒體頁的作品仍須含任一完整目標標籤。",
    ),
    (
        "選目錄，開始收集",
        "選好儲存目錄後按「開始收集」。要中止時按「停止」，等待目前連線結束；紀錄會保留供之後補抓。\n"
        "進階設定可限制起始日期（台灣時間）與本輪貼文數，數量包含已下載者。"
        "X 搜尋與可見範圍有限，無法保證收齊。",
    ),
)
LOGIN_SEQUENCE = "完成登入 → 關閉該專用視窗 → 回 App 按「儲存登入狀態」"
PRIVACY = (
    "本機 data 內的專用瀏覽器設定檔與 X Cookie 含敏感登入資訊，請勿上傳或分享。"
    "本工具不會上傳這些本機檔案；擷取作品仍需向 X 傳送請求與必要的登入 Cookie。"
)
COPYRIGHT = "作品著作權屬原作者；請尊重授權，勿未經同意轉載或公開分享。"
CONNECTION_HELP_URL = "https://developers.cloudflare.com/warp-client/warp-modes/"
THREAD_HELP = (
    "圖片與影片會自動下載。進階設定可開關「補抓原作者在同一討論串的圖片與影片」。"
    "原貼文符合目標標籤後，才補抓同一作者在該串的媒體留言；留言本身可沒有標籤。"
    "其他作者、引用貼文及無法確認關聯的媒體不會因這個選項而下載。"
    "這個例外只用於該討論串，作者媒體頁的其他作品仍逐篇檢查標籤。"
    "X 可能隱藏或限制部分留言，無法保證收齊。"
)
CONNECTION_HELP = (
    "X 載入或下載較慢時，可選用 WARP 本機代理；一般連線正常時不需要開啟。\n"
    "若只想讓本工具的下載經 WARP，請先自行安裝官方 WARP，於 Preferences → Advanced → "
    "Configure Proxy 啟用 Local proxy 並選擇連接埠（選單依版本可能不同）。\n"
    "再到 App「進階設定」勾選 WARP 本機代理，填入相同連接埠（預設 40000）。"
    "只有標籤搜尋、作者補抓與媒體下載經代理；登入瀏覽器沿用作業系統網路設定。\n"
    "App 不會安裝、啟用或切換 WARP，也不會自動更改系統 DNS。"
    "代理失敗會回報，不會自動改為直接連線。\n"
    "1.1.1.1 主要處理 DNS 查詢；WARP 會改變流量路徑。是否改善取決於網路環境，"
    "不能保證 X 變快，也不代表匿名。\n"
    "連線較慢時可先用一般瀏覽器確認 X 能否開啟，暫停其他下載或稍後重試；"
    "已完成的下載紀錄會保留，可再補抓。"
)


class UsageGuideDialog(QDialog):
    """Use show() for first-run help or exec() when opened from a help button."""

    def __init__(self, parent=None, theme: str = "light"):
        super().__init__(parent)
        self.theme_mode = "dark" if theme == "dark" else "light"
        self.setObjectName("usageGuideDialog")
        self.setProperty("i18nSkip", True)
        self.setWindowTitle(tr("FanArt Downloader 使用說明"))
        self.setAccessibleName(tr("FanArt Downloader 四步使用說明"))
        self.setModal(False)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        maximum_width = max(1, available.width() - 48) if available is not None else 760
        maximum_height = max(1, available.height() - 56) if available is not None else 700
        self.setMaximumSize(min(760, maximum_width), maximum_height)
        self.setMinimumSize(min(600, maximum_width), min(440, maximum_height))
        self.resize(min(720, maximum_width), min(650, maximum_height))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 20)
        layout.setSpacing(16)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.guide_icon = QLabel()
        self.guide_icon.setObjectName("usageGuideIcon")
        self.guide_icon.setFixedSize(34, 34)
        self.guide_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.guide_icon)
        self.title_label = self._label("四步開始收集", "usageGuideTitle", "title")
        header.addWidget(self.title_label, 1)
        layout.addLayout(header)
        self.introduction_label = self._label(
            "先設定標籤，再儲存登入狀態。之後可隨時從「使用說明」重看這份引導。",
            "usageGuideIntroduction", "muted",
        )
        layout.addWidget(self.introduction_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("usageGuideScroll")
        self.scroll_area.setAccessibleName(tr("四步操作與登入資料說明"))
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scroll_area.viewport().setObjectName("usageGuideViewport")
        content = QWidget()
        content.setObjectName("usageGuideContent")
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 10, 0)
        body.setSpacing(18)
        self.step_titles = []
        self.step_bodies = []
        self.step_numbers = []
        for number, (title, text) in enumerate(STEPS, 1):
            row = QHBoxLayout()
            row.setSpacing(12)
            label = self._label(str(number), f"usageGuideStep{number}Number", "number")
            label.setFixedWidth(24)
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            row.addWidget(label)
            self.step_numbers.append(label)
            section = QVBoxLayout()
            section.setSpacing(6)
            heading = self._label(title, f"usageGuideStep{number}Title", "stepTitle")
            paragraph = self._label(text, f"usageGuideStep{number}Body", "body")
            self.step_titles.append(heading)
            self.step_bodies.append(paragraph)
            section.addWidget(heading)
            section.addWidget(paragraph)
            if number == 2:
                self.login_sequence_label = self._label(LOGIN_SEQUENCE, "usageGuideLoginSequence", "sequence")
                section.addWidget(self.login_sequence_label)
                self.login_note_label = self._label(
                    "只關閉這個專用登入視窗，保留日常瀏覽器。若仍占用，關好後再按儲存；"
                    "App 重新開啟後也能直接儲存。登入是否仍有效，會在下載時再由 X 確認。",
                    "usageGuideLoginNote", "muted",
                )
                section.addWidget(self.login_note_label)
            row.addLayout(section, 1)
            body.addLayout(row)

        thread_help = QVBoxLayout()
        thread_help.setSpacing(6)
        self.thread_title = self._label("原作者留言補抓", "usageGuideThreadTitle", "stepTitle")
        self.thread_label = self._label(THREAD_HELP, "usageGuideThread", "body")
        thread_help.addWidget(self.thread_title)
        thread_help.addWidget(self.thread_label)
        body.addLayout(thread_help)

        connection = QVBoxLayout()
        connection.setSpacing(6)
        self.connection_title = self._label("連線較慢時", "usageGuideConnectionTitle", "stepTitle")
        self.connection_label = self._label(CONNECTION_HELP, "usageGuideConnection", "body")
        self.connection_help_button = QPushButton(tr("Cloudflare 官方模式說明"))
        self.connection_help_button.setObjectName("usageGuideConnectionHelp")
        self.connection_help_button.setAccessibleName(tr("在瀏覽器開啟 Cloudflare 官方說明"))
        self.connection_help_button.clicked.connect(lambda: self._open_connection_help(CONNECTION_HELP_URL))
        connection.addWidget(self.connection_title)
        connection.addWidget(self.connection_label)
        connection.addWidget(self.connection_help_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addLayout(connection)

        self.privacy_frame = QFrame()
        self.privacy_frame.setObjectName("usageGuidePrivacyPanel")
        privacy = QVBoxLayout(self.privacy_frame)
        privacy.setContentsMargins(14, 12, 14, 13)
        privacy.setSpacing(6)
        self.privacy_title = self._label("登入資料留在本機，請勿分享", "usageGuidePrivacyTitle", "stepTitle")
        self.privacy_label = self._label(PRIVACY, "usageGuidePrivacy", "body")
        privacy.addWidget(self.privacy_title)
        privacy.addWidget(self.privacy_label)
        body.addWidget(self.privacy_frame)
        body.addStretch()
        self.scroll_area.setWidget(content)
        layout.addWidget(self.scroll_area, 1)

        self.copyright_label = self._label(COPYRIGHT, "usageGuideCopyright", "muted")
        layout.addWidget(self.copyright_label)
        footer = QHBoxLayout()
        footer.addStretch()
        self.close_button = QPushButton(tr("關閉說明"))
        self.close_button.setObjectName("usageGuideClose")
        self.close_button.setAccessibleName(tr("關閉使用說明"))
        self.close_button.setMinimumSize(112, 38)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.setTabOrder(self.scroll_area, self.connection_help_button)
        self.setTabOrder(self.connection_help_button, self.close_button)
        self.set_theme(self.theme_mode)
        self.retranslate_ui()

    @staticmethod
    def _label(text: str, name: str, role: str) -> QLabel:
        label = QLabel(tr(text))
        label.setObjectName(name)
        label.setProperty("guideRole", role)
        label.setProperty("guideSource", text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def retranslate_ui(self) -> None:
        """Update this instance without resetting its theme or reading position."""
        position = self.scroll_area.verticalScrollBar().value()
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("FanArt Downloader 使用說明"))
        self.setAccessibleName(tr("FanArt Downloader 四步使用說明"))
        self.scroll_area.setAccessibleName(tr("四步操作與登入資料說明"))
        self.close_button.setText(tr("關閉說明"))
        self.close_button.setAccessibleName(tr("關閉使用說明"))
        self.connection_help_button.setText(tr("Cloudflare 官方模式說明"))
        self.connection_help_button.setAccessibleName(tr("在瀏覽器開啟 Cloudflare 官方說明"))
        for label in self.findChildren(QLabel):
            source = label.property("guideSource")
            if isinstance(source, str):
                label.setText(tr(source))
        self.scroll_area.widget().layout().activate()
        self.scroll_area.verticalScrollBar().setValue(position)

    def _open_connection_help(self, url: str) -> None:
        if url == CONNECTION_HELP_URL:
            QDesktopServices.openUrl(QUrl(url))

    def set_theme(self, theme: str) -> None:
        self.theme_mode = "dark" if theme == "dark" else "light"
        apply_dialog_theme(self, self.theme_mode)
        self.setObjectName("usageGuideDialog")
        c = theme_colors(self.theme_mode)
        self.setStyleSheet(self.styleSheet() + f"""
            QDialog#usageGuideDialog, QWidget#usageGuideContent, QWidget#usageGuideViewport,
            QScrollArea#usageGuideScroll {{ background: {c['window']}; color: {c['ink']}; }}
            QDialog#usageGuideDialog QLabel {{ color: {c['ink']}; background: transparent; font-size: 14px; }}
            QDialog#usageGuideDialog QLabel[guideRole="title"] {{ font-size: 24px; font-weight: 700; }}
            QDialog#usageGuideDialog QLabel[guideRole="stepTitle"] {{ font-size: 15px; font-weight: 600; }}
            QDialog#usageGuideDialog QLabel[guideRole="number"] {{ color: {c['accent']}; font-size: 20px; font-weight: 600; }}
            QDialog#usageGuideDialog QLabel[guideRole="muted"] {{ color: {c['muted']}; font-size: 13px; }}
            QDialog#usageGuideDialog QLabel#usageGuideLoginSequence {{ background: {c['accent_soft']};
                color: {c['accent_soft_ink']}; border-radius: 7px; padding: 10px 12px; font-weight: 600; }}
            QFrame#usageGuidePrivacyPanel {{ background: {c['surface']}; border: none; border-radius: 10px; }}
            QPushButton#usageGuideClose {{ color: {c['accent_ink']}; background: {c['accent']};
                border-color: {c['accent']}; font-weight: 600; }}
            QPushButton#usageGuideClose:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
            QPushButton#usageGuideClose:pressed {{ background: {c['accent_pressed']}; }}
            QPushButton#usageGuideClose:focus {{ border: 2px solid {c['ink']}; padding: 7px 13px; }}
        """)
        self.guide_icon.setPixmap(line_icon("info", 26, c["accent"]).pixmap(QSize(26, 26)))

    apply_theme = set_theme
