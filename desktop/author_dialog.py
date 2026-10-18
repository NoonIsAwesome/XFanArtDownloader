"""Author information using the same theme as the collection window.

Content reference: Singing Stream Savior 2/BGMSavior/BGMSavior.cpp, the
authorInfo section of retranslateUi(), and Translations/bgmsavior_zh_TW.ts.
No information is fetched when this dialog opens.
"""

from html import escape

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from resources_rc import get_author_image_path, get_icon_path
from ui_design import apply_dialog_theme, theme_colors
from i18n import tr, locale as ui_locale


# 作者連結集中列在程式內，也作為允許開啟網址的清單；此頁不會背景抓取資料。
# Fixed author links also form the URL allowlist; opening this page fetches nothing.
SOCIAL_LINKS = (
    ("官方網站", "https://noonisawesome.dev/"),
    ("YouTube · @NoonVtuber", "https://www.youtube.com/@NoonVtuber"),
    ("X / Twitter · @NoonVtuber", "https://twitter.com/NoonVtuber"),
    ("Twitch · NoonVtuber", "https://www.twitch.tv/NoonVtuber"),
    ("Threads · @noonvtuber", "https://www.threads.com/@noonvtuber"),
    ("Instagram · @noonvtuber", "https://www.instagram.com/noonvtuber"),
)
SUPPORT_LINKS = (
    ("綠界 ECPay", "https://p.ecpay.com.tw/3604623"),
    ("PayPal", "https://streamlabs.com/noon_0606/tip"),
)
COLLABORATION_LINKS = (
    ("請填寫合作聯絡表單", "https://reurl.cc/1k0W9Q"),
)
BIOGRAPHY = (
    "我曾經是一個 NPC，長期與路過的旅人對話讓我獲得了自我意識。\n"
    "平常喜歡唱歌、彈吉他和玩遊戲，主要在 YouTube 上直播活動，歡迎你來找我玩！"
)
COPYRIGHT = "Copyright © 2026 Noon Is Awesome. No rights reserved."


class _AuthorLinkLabel(QLabel):
    """One focusable link, with activation independent of text-anchor selection."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.linkActivated.emit(self.url)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, forward):
        # 每個標籤只有一個連結；略過 QLabel 的額外錨點，讓一次 Tab 前進一項。
        # Each label has one link; skip QLabel's extra anchor stop so one Tab advances once.
        return QWidget.focusNextPrevChild(self, forward)


class AuthorInfoDialog(QDialog):
    def __init__(self, parent=None, theme: str | None = None):
        super().__init__(parent)
        # 預設沿用主視窗主題；翻譯由本頁管理，切換語言時保留連結與捲動狀態。
        # Inherit the parent theme and retranslate locally without losing links or scroll state.
        self.theme_mode = self._normalise_theme(
            theme if theme is not None else getattr(parent, "theme_mode", "light")
        )
        self.setObjectName("authorDialog")
        self.setProperty("i18nSkip", True)
        self.setWindowTitle(tr("作者資訊"))
        self.setAccessibleName(tr("作者資訊：沐橙 Noon"))
        self.setWindowIcon(QIcon(get_icon_path()))
        self.resize(620, 660)
        self.setMinimumSize(500, 460)
        self._links: list[tuple[QLabel, str, str]] = []
        self._translated_labels: list[tuple[QLabel, str]] = []
        self._allowed_urls = {url for _, url in SOCIAL_LINKS + SUPPORT_LINKS + COLLABORATION_LINKS}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(18)
        self.avatar_label = QLabel()
        self.avatar_label.setObjectName("authorAvatar")
        self.avatar_label.setAccessibleName(tr("沐橙 Noon 作者頭像"))
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_label.setFixedSize(88, 88)
        pixmap = QPixmap(get_author_image_path())
        if pixmap.isNull():
            self.avatar_label.setText("Noon")
        else:
            self.avatar_label.setPixmap(pixmap.scaled(
                80, 80, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        header.addWidget(self.avatar_label)
        introduction = QVBoxLayout()
        introduction.setSpacing(6)
        introduction.addStretch()
        self.name_label = self._text("沐橙 Noon", "name")
        introduction.addWidget(self.name_label)
        introduction.addWidget(self._text("Good afterNoon！我是沐橙！", "greeting"))
        introduction.addStretch()
        header.addLayout(introduction, 1)
        layout.addLayout(header)

        # 介紹與社群清單可捲動，頁尾的關閉入口不會跟著長內容移出畫面。
        # Scroll biography/social content while keeping the footer's Close action visible.
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("authorScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.viewport().setObjectName("authorViewport")
        self.scroll_area.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("authorContent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 10, 0)
        body.setSpacing(10)
        self.biography_label = self._text(BIOGRAPHY, "body")
        self.biography_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.biography_label)
        body.addWidget(self._text("「這位勇者，你願意接下我的任務嗎？」", "quote"))
        body.addSpacing(4)
        divider = QFrame()
        divider.setObjectName("authorDivider")
        divider.setFixedHeight(1)
        body.addWidget(divider)
        self._add_link_section(body, "社群連結", SOCIAL_LINKS)
        self._add_link_section(body, "支持我", SUPPORT_LINKS)
        self._add_link_section(body, "合作聯絡", COLLABORATION_LINKS)
        body.addStretch()
        self.scroll_area.setWidget(content)
        layout.addWidget(self.scroll_area, 1)

        self.copyright_label = self._text(COPYRIGHT, "copyright")
        self.copyright_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.copyright_label)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch()
        self.close_button = QPushButton(tr("關閉"))
        self.close_button.setObjectName("authorClose")
        self.close_button.setAccessibleName(tr("關閉作者資訊"))
        self.close_button.setMinimumSize(96, 36)
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        for previous, current in zip(self._links, self._links[1:]):
            self.setTabOrder(previous[0], current[0])
        self.setTabOrder(self._links[-1][0], self.close_button)
        self.apply_theme(self.theme_mode)
        self.retranslate_ui()

    @staticmethod
    def _normalise_theme(theme: str) -> str:
        return "dark" if theme == "dark" else "light"

    def _text(self, text: str, role: str) -> QLabel:
        # 介紹內容用純文字，並記下原文，避免多次切換語言後失去翻譯來源。
        # Render biography as plain text and retain source keys across language switches.
        label = QLabel(tr(text))
        label.setProperty("authorRole", role)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._translated_labels.append((label, text))
        return label

    def _add_link_section(self, layout: QVBoxLayout, title: str, links: tuple) -> None:
        layout.addSpacing(6)
        layout.addWidget(self._text(title, "section"))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        for index, (text, url) in enumerate(links):
            label = _AuthorLinkLabel(url)
            label.setProperty("authorLink", True)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
            )
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            label.setOpenExternalLinks(False)
            label.setAccessibleName(tr("開啟{label}", label=tr(text)))
            label.setToolTip(url)
            label.setWordWrap(True)
            label.linkActivated.connect(self._open_link)
            self._links.append((label, text, url))
            grid.addWidget(label, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

    def _open_link(self, url: str) -> None:
        # 只開啟上方列出的固定網址，不接受任意 linkActivated 內容。
        # Open only the fixed URLs above, not arbitrary linkActivated input.
        if url in self._allowed_urls:
            QDesktopServices.openUrl(QUrl(url))

    def retranslate_ui(self) -> None:
        """Keep links, focus and scroll state while replacing visible language."""
        position = self.scroll_area.verticalScrollBar().value()
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("作者資訊"))
        self.setAccessibleName(tr("作者資訊：沐橙 Noon"))
        self.avatar_label.setAccessibleName(tr("沐橙 Noon 作者頭像"))
        self.close_button.setText(tr("關閉"))
        self.close_button.setAccessibleName(tr("關閉作者資訊"))
        for label, source in self._translated_labels:
            label.setText(tr(source))
        self._render_links()
        self.scroll_area.widget().layout().activate()
        self.scroll_area.verticalScrollBar().setValue(position)

    def _render_links(self) -> None:
        # 連結需要 HTML 外觀，但網址與譯文都先轉義，避免文字變成額外標記。
        # Escape both URLs and translated labels before producing link HTML.
        colors = theme_colors(self.theme_mode)
        for label, source, url in self._links:
            text = tr(source)
            label.setText(
                f'<a href="{escape(url, quote=True)}" '
                f'style="color:{colors["accent"]}; text-decoration:underline;">{escape(text)}</a>'
            )
            label.setAccessibleName(tr("開啟{label}", label=text))

    def apply_theme(self, theme: str) -> None:
        # 同步主題時沿用現有標籤與連結，保留鍵盤焦點及既有訊號連接。
        # Reuse labels/links when changing themes, preserving keyboard focus and connections.
        self.theme_mode = self._normalise_theme(theme)
        apply_dialog_theme(self, self.theme_mode)
        self.setObjectName("authorDialog")
        colors = theme_colors(self.theme_mode)
        self.setStyleSheet(self.styleSheet() + f"""
            QDialog#authorDialog, QWidget#authorContent, QWidget#authorViewport,
            QScrollArea#authorScroll {{ background: {colors['window']}; color: {colors['ink']}; }}
            QDialog#authorDialog QLabel {{ color: {colors['ink']}; background: transparent; font-size: 14px; }}
            QDialog#authorDialog QLabel[authorRole="name"] {{ font-size: 25px; font-weight: 700; }}
            QDialog#authorDialog QLabel[authorRole="greeting"] {{ color: {colors['muted']}; }}
            QDialog#authorDialog QLabel[authorRole="body"] {{ padding: 2px 0; }}
            QDialog#authorDialog QLabel[authorRole="quote"] {{ font-weight: 600; padding: 4px 0; }}
            QDialog#authorDialog QLabel[authorRole="section"] {{ color: {colors['muted']}; font-size: 13px; font-weight: 600; }}
            QDialog#authorDialog QLabel[authorRole="copyright"] {{ color: {colors['muted']}; font-size: 11px; }}
            QDialog#authorDialog QLabel#authorAvatar {{ background: {colors['accent_soft']}; border-radius: 12px; }}
            QFrame#authorDivider {{ background: {colors['divider']}; border: none; }}
            QDialog#authorDialog QLabel[authorLink="true"] {{ border: 1px solid transparent; border-radius: 5px; padding: 5px 4px; }}
            QDialog#authorDialog QLabel[authorLink="true"]:hover {{ background: {colors['hover']}; }}
            QDialog#authorDialog QLabel[authorLink="true"]:focus {{ background: {colors['accent_soft']}; border-color: {colors['accent']}; }}
            QPushButton#authorClose {{ background: {colors['accent']}; color: {colors['accent_ink']};
                border: 1px solid {colors['accent']}; border-radius: 7px; padding: 6px 14px; }}
            QPushButton#authorClose:hover {{ background: {colors['accent_hover']}; }}
            QPushButton#authorClose:pressed {{ background: {colors['accent_pressed']}; }}
            QPushButton#authorClose:focus {{ border: 2px solid {colors['ink']}; padding: 5px 13px; }}
        """)
        self._render_links()
