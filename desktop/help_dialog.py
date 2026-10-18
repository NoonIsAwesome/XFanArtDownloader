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


# 操作說明集中保存繁中原文，作為翻譯查找的穩定來源；開啟說明不會連網。
# Keep Traditional Chinese source copy as stable translation keys; opening help is offline.
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
        "進階設定可設起始日期（台灣時間）與「搜尋貼文上限」；達搜尋上限後仍會繼續作者補抓。"
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
    "遇到 X 限制討論串請求頻率時，本輪會暫停後續留言補抓，搜尋與作者媒體仍會繼續；"
    "未完成的留言可稍後重試。"
)
SEARCH_LIMIT_HELP = (
    "「搜尋貼文上限」只計算所有標籤搜尋共用、不重複且符合標籤與日期的原貼文，包含已下載者。"
    "達上限只停止標籤搜尋，作者媒體補抓仍繼續。\n"
    "貼文網址、失敗重試、作者媒體頁與同作者留言附件不占搜尋配額；"
    "總處理貼文數與媒體下載數可能超過搜尋上限。作者媒體頁仍逐篇核對標籤與日期，"
    "這個設定不代表已收齊作品。"
)
PREVIEW_HELP = (
    "目前作者的公開頭像請求不帶登入 Cookie；讀取失敗會顯示替代圖，不影響下載。"
    "主頁只顯示作者與簡要進度；按「查看紀錄與預覽」，可在「紀錄與圖片預覽」選取一筆完成紀錄預覽本機圖片；「詳細下載狀況」顯示完整執行紀錄與來源進度。"
    "預覽不會重新下載或上傳；影片不會自動播放。\n"
    "紀錄中的 🖼️／🎬 分別表示圖片與影片。啟用作者資料夾時，簡短路徑顯示「作者帳號\\檔名」；"
    "關閉時只顯示檔名。"
)
RESUME_HELP = (
    "設定起始日期並勾選分段搜尋，會以 30 天為一段查詢，仍遵守搜尋上限。\n"
    "相同設定可接續未完成工作；已完成來源會略過，未完成段落可能重新掃描，已下載媒體不會重複下載。"
    "接續時，此工作已看過的搜尋貼文不重占本輪配額。\n"
    "未完成工作沿用原先排程的日期範圍；若要立即查詢新日期，可取消「接續未完成的收集工作」開始新工作。"
)
TOOLS_HELP = (
    "「更多功能 → 下載紀錄與收集進度」可篩選下載結果、開啟檔案，以及只重試選取的失敗項目；"
    "收集進度分頁會列出尚未完成的來源。\n"
    "「清除本機登入資料」需再次確認，只移除本工具的 Cookie 與專用瀏覽器資料，保留下載與紀錄。"
    "「檢查 App 與擷取工具更新」只查詢官方版本資訊，不會自動下載或安裝。"
)
# 明確區分失敗重試與已完成項目的主動重抓，並在操作前說明原檔保護。
# Distinguish failed retries from intentional redownloads and explain original-file protection.
REDOWNLOAD_HELP = (
    "在下載紀錄選取已完成項目，按「重新下載選取項目」。預設另存副本，在原資料夾建立不重複的新檔名。\n"
    "覆寫原檔需再次確認；新檔完整下載並驗證成功後才替換，失敗或停止時保留原檔。"
    "只處理選取項目，不會重新搜尋其他貼文。原檔已遺失或損壞時，請重新掃描來源貼文補抓。"
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
        # 由本視窗自行重譯各段，避免通用元件樹覆蓋原文或重建閱讀位置。
        # Retranslate this dialog locally, preserving source keys and reading position.
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

        # 長篇步驟放在可捲動區，標題與底部關閉按鈕維持可見。
        # Scroll the long instructions while keeping the title and Close action visible.
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

        # 將同串例外、搜尋配額與預覽分開說明，避免被誤認為全面放寬標籤。
        # Explain thread exceptions, search quotas, and previews separately from tag matching.
        thread_help = QVBoxLayout()
        thread_help.setSpacing(6)
        self.thread_title = self._label("原作者留言補抓", "usageGuideThreadTitle", "stepTitle")
        self.thread_label = self._label(THREAD_HELP, "usageGuideThread", "body")
        thread_help.addWidget(self.thread_title)
        thread_help.addWidget(self.thread_label)
        body.addLayout(thread_help)

        search_limit = QVBoxLayout()
        search_limit.setSpacing(6)
        self.search_limit_title = self._label("搜尋貼文上限的範圍", "usageGuideSearchLimitTitle", "stepTitle")
        self.search_limit_label = self._label(SEARCH_LIMIT_HELP, "usageGuideSearchLimit", "body")
        search_limit.addWidget(self.search_limit_title)
        search_limit.addWidget(self.search_limit_label)
        body.addLayout(search_limit)

        preview_help = QVBoxLayout()
        preview_help.setSpacing(6)
        self.preview_title = self._label("作者與本機預覽", "usageGuidePreviewTitle", "stepTitle")
        self.preview_label = self._label(PREVIEW_HELP, "usageGuidePreview", "body")
        preview_help.addWidget(self.preview_title)
        preview_help.addWidget(self.preview_label)
        body.addLayout(preview_help)

        # 將續抓限制與管理入口寫進內建說明，第一次使用也不必另找 README。
        # Explain resume boundaries and management tools in the first-run guide as well as the README.
        for title, text, name in (
            ("分段搜尋與接續工作", RESUME_HELP, "Resume"),
            ("紀錄、登入資料與更新", TOOLS_HELP, "Tools"),
            ("重新下載已完成項目", REDOWNLOAD_HELP, "Redownload"),
        ):
            section = QVBoxLayout()
            section.setSpacing(6)
            section.addWidget(self._label(title, f"usageGuide{name}Title", "stepTitle"))
            section.addWidget(self._label(text, f"usageGuide{name}Body", "body"))
            body.addLayout(section)

        # 連線說明是本機文字；只有使用者點按官方連結時才開啟瀏覽器。
        # Network guidance is local text; only an explicit link click opens a browser.
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
        # 即使目前顯示其他語言，也能從同一份原文重新翻譯。
        # Keep the original translation key even when another language is displayed.
        label.setProperty("guideSource", text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def retranslate_ui(self) -> None:
        """Update this instance without resetting its theme or reading position."""
        # 先記錄捲動位置，待新文字排版完成後恢復，保留閱讀進度。
        # Restore the reading position after translated text has been laid out.
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
        # 外部連結只接受程式內固定的官方網址，不從顯示文字推導目標。
        # Accept only the fixed official URL, never a target inferred from displayed text.
        if url == CONNECTION_HELP_URL:
            QDesktopServices.openUrl(QUrl(url))

    def set_theme(self, theme: str) -> None:
        # 只替換配色與圖示，不重建元件、訊號或說明內容。
        # Change colors/icons without rebuilding widgets, connections, or help content.
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
            QPushButton#usageGuideClose[keyboardFocus="true"]:focus {{ border: 2px solid {c['ink']}; padding: 7px 13px; }}
        """)
        self.guide_icon.setPixmap(line_icon("info", 26, c["accent"]).pixmap(QSize(26, 26)))

    apply_theme = set_theme
