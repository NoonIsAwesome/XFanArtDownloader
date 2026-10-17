"""Bright gallery layout for the existing downloader widgets.

Integration: call ``build_gallery_layout(window)`` from setup_ui. The function
does not replace any input, button, or status_box created by the controller.
``refresh_gallery_summary(window)`` is safe during validation and updates only
tag/source/path summaries. The controller owns run_state_label,
result_detail_label, and login_state_label. Log emptiness follows textChanged.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QProxyStyle, QPushButton,
    QScrollArea, QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget,
)


from ui_theme import apply_dialog_theme, apply_palette, gallery_stylesheet, theme_colors


TEAL = "#176C68"
INK = "#202A30"
MUTED = "#586971"


class GalleryControlStyle(QProxyStyle):
    """Keep real Qt checkbox behavior while drawing a clear teal check mark."""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight):
            return 18
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            return super().drawPrimitive(element, option, painter, widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        colors = getattr(self, "colors", theme_colors())
        color = colors["accent"] if enabled else colors["disabled_bg"]
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(color if checked else colors["border"]), 1))
        painter.setBrush(QColor(color if checked else colors["input"]))
        painter.drawRoundedRect(rect, 3, 3)
        if checked:
            painter.setPen(QPen(QColor(colors["accent_ink"] if enabled else colors["disabled_fg"]), 1.8, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath(QPointF(rect.left() + rect.width() * .22, rect.center().y()))
            path.lineTo(rect.left() + rect.width() * .43, rect.top() + rect.height() * .70)
            path.lineTo(rect.left() + rect.width() * .79, rect.top() + rect.height() * .29)
            painter.drawPath(path)
        painter.restore()


def line_icon(name: str, size: int = 20, color: str = TEAL) -> QIcon:
    """Small authored line icons, rendered natively at twice the target size."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size * 2 / 24, size * 2 / 24)
    pen = QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "gallery":
        painter.drawRoundedRect(QRectF(2.5, 2.5, 19, 19), 2.2, 2.2)
        path = QPainterPath(QPointF(5.5, 16.6))
        path.lineTo(10.2, 12.7)
        path.lineTo(15, 16.6)
        path.lineTo(17.2, 15)
        path.lineTo(20, 17.2)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#F48472"))
        painter.drawEllipse(QPointF(16.1, 8.2), 2.2, 2.2)
    elif name == "folder":
        path = QPainterPath(QPointF(3, 6.5))
        path.lineTo(9, 6.5)
        path.lineTo(11.4, 9)
        path.lineTo(21, 9)
        path.lineTo(21, 19)
        path.lineTo(3, 19)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(3, 10), QPointF(20.5, 10))
    elif name == "login":
        painter.drawRoundedRect(QRectF(12, 4, 8, 16), 1.5, 1.5)
        painter.drawLine(QPointF(3, 12), QPointF(15, 12))
        painter.drawLine(QPointF(11, 8), QPointF(15, 12))
        painter.drawLine(QPointF(11, 16), QPointF(15, 12))
    elif name == "save":
        path = QPainterPath(QPointF(5, 3.5))
        path.lineTo(17, 3.5)
        path.lineTo(20.5, 7)
        path.lineTo(20.5, 20.5)
        path.lineTo(3.5, 20.5)
        path.lineTo(3.5, 3.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawRect(QRectF(8, 3.5, 8, 6))
        painter.drawRoundedRect(QRectF(7, 14, 10, 6.5), 1, 1)
    elif name == "download":
        painter.drawLine(QPointF(12, 3), QPointF(12, 15))
        painter.drawLine(QPointF(7.5, 10.5), QPointF(12, 15))
        painter.drawLine(QPointF(16.5, 10.5), QPointF(12, 15))
        painter.drawLine(QPointF(4, 16), QPointF(4, 21))
        painter.drawLine(QPointF(4, 21), QPointF(20, 21))
        painter.drawLine(QPointF(20, 21), QPointF(20, 16))
    elif name == "stop":
        painter.drawRoundedRect(QRectF(6, 6, 12, 12), 1.5, 1.5)
    elif name == "info":
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        painter.drawLine(QPointF(12, 10.5), QPointF(12, 16.5))
        painter.drawPoint(QPointF(12, 7))
    elif name == "calendar":
        painter.drawRoundedRect(QRectF(3, 5, 18, 16), 2, 2)
        painter.drawLine(QPointF(3, 10), QPointF(21, 10))
        painter.drawLine(QPointF(7, 3), QPointF(7, 7))
        painter.drawLine(QPointF(17, 3), QPointF(17, 7))
        for x, y in ((7, 14), (12, 14), (17, 14), (7, 18), (12, 18)):
            painter.drawPoint(QPointF(x, y))
    elif name in ("chevron-left", "chevron-right"):
        x1, x2 = (14.5, 9.5) if name == "chevron-left" else (9.5, 14.5)
        path = QPainterPath(QPointF(x1, 6))
        path.lineTo(x2, 12)
        path.lineTo(x1, 18)
        painter.drawPath(path)
    elif name == "sun":
        painter.drawEllipse(QRectF(7, 7, 10, 10))
        for x1, y1, x2, y2 in ((12, 2, 12, 4), (12, 20, 12, 22), (2, 12, 4, 12), (20, 12, 22, 12),
                                  (5, 5, 6.5, 6.5), (17.5, 17.5, 19, 19), (5, 19, 6.5, 17.5), (17.5, 6.5, 19, 5)):
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    elif name == "moon":
        path = QPainterPath(QPointF(17.8, 3.8))
        path.cubicTo(8, 1.5, 2.5, 10, 6, 17)
        path.cubicTo(11, 24, 22, 18, 20, 12.8)
        path.cubicTo(11, 17, 7, 6, 17.8, 3.8)
        painter.drawPath(path)
    elif name == "tray":
        path = QPainterPath(QPointF(4, 11))
        path.lineTo(8, 4)
        path.lineTo(16, 4)
        path.lineTo(20, 11)
        path.lineTo(20, 19)
        path.lineTo(4, 19)
        path.closeSubpath()
        painter.drawPath(path)
        path = QPainterPath(QPointF(4, 12))
        path.lineTo(9, 12)
        path.lineTo(10.5, 14.4)
        path.lineTo(13.5, 14.4)
        path.lineTo(15, 12)
        path.lineTo(20, 12)
        painter.drawPath(path)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def _label(text: str, role: str = "body", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return widget


def _rule() -> QFrame:
    widget = QFrame()
    widget.setObjectName("galleryRule")
    widget.setFixedHeight(1)
    return widget


def apply_gallery_theme(window, mode=None):
    mode = mode if mode in ("light", "dark") else getattr(window, "theme_mode", "light")
    window.theme_mode = mode
    colors = theme_colors(mode)
    font = QFont()
    font.setFamilies(["Microsoft JhengHei UI", "Segoe UI"])
    font.setPointSizeF(10)
    window.setFont(font)
    window.setObjectName("galleryWindow")
    apply_palette(window, mode)
    window.setStyleSheet(gallery_stylesheet(mode))
    if hasattr(window, "_gallery_control_style"):
        window._gallery_control_style.colors = colors
        for checkbox in window.findChildren(QCheckBox):
            checkbox.update()
    for attr, name, color in (
        ("cookie_btn", "login", "accent_ink"), ("cookie_save_btn", "save", "accent"),
        ("cookie_import_btn", "folder", "accent"),
        ("choose_path_btn", "folder", "accent"), ("download_btn", "download", "accent_ink"),
        ("stop_btn", "stop", "muted"), ("about_btn", "info", "accent"),
        ("help_btn", "info", "accent"),
    ):
        if hasattr(window, attr):
            getattr(window, attr).setIcon(line_icon(name, color=colors[color]))
    for attr, icon, size, color in (
        ("logo_mark", "gallery", 48, "accent"), ("log_tray", "tray", 52, "muted"),
        ("strict_info", "info", 20, "accent"),
    ):
        if hasattr(window, attr):
            getattr(window, attr).setPixmap(line_icon(icon, size, colors[color]).pixmap(QSize(size, size)))
    for editor_attr in ("hashtag_input", "authors_input", "tweet_urls_input"):
        if hasattr(window, editor_attr):
            editor = getattr(window, editor_attr)
            palette = editor.palette()
            palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["muted"]))
            editor.setPalette(palette)
    if hasattr(window, "limit_date_input") and hasattr(window.limit_date_input, "set_theme"):
        window.limit_date_input.set_theme(mode)
    if hasattr(window, "theme_btn"):
        target = "淺色" if mode == "dark" else "深色"
        window.theme_btn.setText(target + "模式")
        window.theme_btn.setAccessibleName("切換為" + target + "模式")
        window.theme_btn.setToolTip("目前為" + ("深色" if mode == "dark" else "淺色") + "模式；按一下切換並記住選擇。")
        window.theme_btn.setIcon(line_icon("sun" if mode == "dark" else "moon", color=colors["accent"]))
    if hasattr(window, "promotion_link"):
        window.promotion_link.setText(
            '<a href="https://noonisawesome.dev/" style="color:' + colors["accent"] + '">'
            '試試超讚的歌回直播軟體</a>'
        )


def set_window_theme(window, mode):
    apply_gallery_theme(window, mode if mode in ("light", "dark") else "light")


def refresh_gallery_summary(window):
    """Update summaries without raising while the user is editing a tag."""
    text = window.hashtag_input.text().strip()
    try:
        from downloader import parse_hashtags
        tags = tuple(parse_hashtags(text))
    except ImportError:
        tags = tuple(dict.fromkeys(piece.lstrip("#") for piece in re.split(r"[\s,，]+", text) if piece.lstrip("#")))
    except (TypeError, ValueError):
        tags = ()
    if tags:
        window.tag_summary_label.setText("  ".join("#" + tag.lstrip("#") for tag in tags))
    else:
        window.tag_summary_label.setText("請檢查標籤" if text else "尚未填寫標籤")
    window.tag_summary_label.setToolTip(window.tag_summary_label.text())
    sources = []
    if window.search_checkbox.isChecked():
        sources.append("標籤搜尋")
    if window.author_media_checkbox.isChecked():
        sources.append("作者媒體補抓")
    if window.tweet_urls_input.toPlainText().strip():
        sources.append("貼文網址")
    if not sources and window.retry_failed_checkbox.isChecked():
        sources.append("重試未完成")
    window.source_summary_label.setText("、".join(sources) if sources else "請選擇收集來源")
    window.path_label.setText(str(window.download_path))
    window.path_label.setToolTip(str(window.download_path))


def build_gallery_layout(window):
    """Build approved split workbench A using all controller-owned widgets."""
    apply_gallery_theme(window)
    window._gallery_control_style = GalleryControlStyle("Fusion")
    window._gallery_control_style.setParent(window)
    window.setWindowTitle("FanArt Downloader")
    window.setMinimumSize(1020, 740)
    window.resize(1160, 860)
    root = QVBoxLayout(window)
    root.setContentsMargins(26, 22, 26, 22)
    root.setSpacing(20)

    header = QHBoxLayout()
    header.setSpacing(14)
    brand_mark = QLabel()
    window.logo_mark = brand_mark
    brand_mark.setPixmap(line_icon("gallery", 48).pixmap(QSize(48, 48)))
    brand_mark.setFixedSize(48, 48)
    header.addWidget(brand_mark)
    brand = QVBoxLayout()
    brand.setSpacing(3)
    brand.addWidget(_label("FanArt Downloader", "brand"))
    brand.addWidget(_label("收集帶有指定標籤的作品", "muted"))
    header.addLayout(brand)
    header.addStretch(1)
    auth = QVBoxLayout()
    auth.setSpacing(6)
    auth_buttons = QHBoxLayout()
    auth_buttons.setSpacing(9)
    window.cookie_btn.setText("開啟瀏覽器登入")
    window.cookie_btn.setIcon(line_icon("login", color="#FFFFFF"))
    window.cookie_btn.setProperty("variant", "primary")
    window.cookie_save_btn.setText("儲存登入狀態")
    window.cookie_save_btn.setIcon(line_icon("save"))
    window.cookie_btn.setToolTip("以一般 Chrome／Edge 模式開啟本工具專用視窗；請在網站中輸入帳號與密碼。")
    window.cookie_save_btn.setToolTip("先關閉專用登入視窗，再讀取同一個本機 profile 的 X 登入狀態。重新開啟 App 後也能直接儲存。")
    window.cookie_import_btn.setText("匯入 Cookie 檔")
    window.cookie_import_btn.setIcon(line_icon("folder"))
    window.cookie_import_btn.setToolTip("選擇 Netscape Cookie TXT 或舊版 cookies.json；與登入後儲存是不同操作。")
    window.theme_btn = QPushButton("深色模式")
    window.about_btn = QPushButton("作者資訊")
    window.help_btn = QPushButton("使用說明")
    window.help_btn.setAccessibleName("開啟使用說明與登入引導")
    utilities = QHBoxLayout()
    utilities.setSpacing(9)
    utilities.addStretch(1)
    utilities.addWidget(window.theme_btn)
    utilities.addWidget(window.help_btn)
    utilities.addWidget(window.about_btn)
    auth.addLayout(utilities)
    auth_buttons.addWidget(window.cookie_btn)
    auth_buttons.addWidget(window.cookie_save_btn)
    auth_buttons.addWidget(window.cookie_import_btn)
    auth.addLayout(auth_buttons)
    window.login_state_label = _label("尚未儲存登入資料", "muted")
    window.login_state_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    window.login_state_label.setWordWrap(True)
    window.login_state_label.setToolTip("本機已儲存 Cookie 不代表已驗證 X 伺服器登入是否仍有效。")
    auth.addWidget(window.login_state_label)
    window.login_help_label = _label(
        "登入完成後，請關閉剛才的專用瀏覽器視窗，再回來按儲存。\n"
        "專用瀏覽器設定檔與 X Cookie 只保留在本機 data，請勿分享。", "muted", wrap=True,
    )
    window.login_help_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    window.login_help_label.setMaximumWidth(530)
    auth.addWidget(window.login_help_label)
    header.addLayout(auth)
    root.addLayout(header)
    root.addWidget(_rule())

    body = QHBoxLayout()
    body.setSpacing(24)
    window.collection_scroll = QScrollArea()
    window.collection_scroll.setWidgetResizable(True)
    window.collection_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.collection_scroll.setMinimumWidth(525)
    left = QWidget()
    form = QVBoxLayout(left)
    form.setContentsMargins(0, 0, 8, 4)
    form.setSpacing(14)
    form.addWidget(_label("收集 FanArt", "title"))
    tags = QVBoxLayout()
    tags.setSpacing(7)
    tag_label = _label("目標標籤", "field")
    tag_label.setBuddy(window.hashtag_input)
    tags.addWidget(tag_label)
    window.hashtag_input.setMinimumHeight(27)
    window.hashtag_input.setPlaceholderText("#角色名稱 #作品標籤")
    window.hashtag_input.setAccessibleName("目標標籤，可填多個，以空格或逗號分隔")
    tags.addWidget(window.hashtag_input)
    tags.addWidget(_label("多個標籤以空格或逗號分隔；符合任一完整標籤即收集，重複標籤會合併。", "muted", wrap=True))
    form.addLayout(tags)

    search = QFrame()
    search.setObjectName("searchSurface")
    search_row = QHBoxLayout(search)
    search_row.setContentsMargins(16, 13, 16, 13)
    search_row.setSpacing(16)
    window.search_checkbox.setText("標籤搜尋")
    window.search_checkbox.setProperty("role", "source")
    search_row.addWidget(window.search_checkbox)
    search_row.addWidget(_label("搜尋含指定標籤的貼文。", "muted", wrap=True), 1)
    form.addWidget(search)

    authors = QFrame()
    authors.setObjectName("authorSurface")
    author_layout = QVBoxLayout(authors)
    author_layout.setContentsMargins(16, 14, 16, 16)
    author_layout.setSpacing(9)
    window.author_media_checkbox.setText("作者媒體補抓")
    window.author_media_checkbox.setProperty("role", "source")
    author_layout.addWidget(window.author_media_checkbox)
    auto = QVBoxLayout()
    auto.setContentsMargins(26, 0, 0, 0)
    auto.setSpacing(5)
    window.discover_authors_checkbox.setText("自動補抓已發現的作者")
    auto.addWidget(window.discover_authors_checkbox)
    auto.addWidget(_label("包含歷史紀錄與本次搜尋找到的作者，可以不填帳號。", "muted", wrap=True))
    author_layout.addLayout(auto)
    manual_label = _label("手動新增作者（選填）", "field")
    manual_label.setBuddy(window.authors_input)
    author_layout.addWidget(manual_label)
    author_layout.addWidget(_label("可填 @帳號 或 https://x.com/作者/media", "muted", wrap=True))
    author_layout.addWidget(_label("搜尋沒找到的作者可在此補上；每行一位，不需要數字 ID。", "muted", wrap=True))
    window.authors_input.setMinimumHeight(62)
    window.authors_input.setMaximumHeight(78)
    window.authors_input.setPlaceholderText("@作者帳號\n或 https://x.com/作者/media")
    author_layout.addWidget(window.authors_input)
    form.addWidget(authors)

    urls = QVBoxLayout()
    urls.setSpacing(6)
    url_label = _label("貼文網址（選填）", "field")
    url_label.setBuddy(window.tweet_urls_input)
    urls.addWidget(url_label)
    window.tweet_urls_input.setMinimumHeight(56)
    window.tweet_urls_input.setMaximumHeight(72)
    window.tweet_urls_input.setPlaceholderText("https://x.com/作者/status/貼文編號\n每行一則貼文網址")
    urls.addWidget(window.tweet_urls_input)
    urls.addWidget(_label("加入已找到的貼文，仍會核對完整標籤。", "muted", wrap=True))
    form.addLayout(urls)
    strict = QFrame()
    strict.setObjectName("strictSurface")
    strict_row = QHBoxLayout(strict)
    strict_row.setContentsMargins(12, 10, 12, 10)
    strict_row.setSpacing(9)
    info = QLabel()
    window.strict_info = info
    info.setPixmap(line_icon("info").pixmap(QSize(20, 20)))
    info.setFixedSize(20, 20)
    strict_row.addWidget(info)
    strict_row.addWidget(_label("所有來源都只下載含指定完整標籤的貼文；作者其他作品會略過。", "body", wrap=True), 1)
    form.addWidget(strict)
    form.addStretch(1)
    window.collection_scroll.setWidget(left)
    body.addWidget(window.collection_scroll, 3)

    divider = QFrame()
    divider.setFixedWidth(1)
    divider.setObjectName("galleryRule")
    body.addWidget(divider)
    right = QWidget()
    right.setMinimumWidth(350)
    right_layout = QVBoxLayout(right)
    right_layout.setContentsMargins(0, 0, 0, 0)
    right_layout.setSpacing(12)
    # The actions and advanced controls keep their own space. Summary/logs can
    # scroll when the window is short or the advanced section is expanded.
    window.sidebar_scroll = QScrollArea()
    window.sidebar_scroll.setWidgetResizable(True)
    window.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.sidebar_scroll.setMinimumHeight(155)
    summary = QWidget()
    sidebar = QVBoxLayout(summary)
    sidebar.setContentsMargins(0, 0, 0, 0)
    sidebar.setSpacing(13)
    sidebar.addWidget(_label("本次收集", "section"))
    tag_row = QHBoxLayout()
    window.tag_summary_label = _label("", wrap=True)
    window.tag_summary_label.setObjectName("tagSummary")
    window.tag_summary_label.setMaximumHeight(78)
    tag_row.addWidget(window.tag_summary_label)
    tag_row.addStretch(1)
    sidebar.addLayout(tag_row)
    window.run_state_label = _label("尚未開始")
    window.run_state_label.setObjectName("runState")
    sidebar.addWidget(window.run_state_label)
    window.result_detail_label = _label("開始後會在下方顯示下載進度。", "muted", wrap=True)
    sidebar.addWidget(window.result_detail_label)
    window.source_summary_label = _label("", "muted", wrap=True)
    sidebar.addWidget(window.source_summary_label)
    sidebar.addWidget(_rule())
    sidebar.addWidget(_label("下載位置", "field"))
    destination = QFrame()
    destination.setObjectName("destinationSurface")
    destination_row = QHBoxLayout(destination)
    destination_row.setContentsMargins(10, 0, 0, 0)
    destination_row.setSpacing(6)
    window.path_label.setTextFormat(Qt.TextFormat.PlainText)
    window.path_label.setWordWrap(True)
    window.path_label.setMinimumWidth(0)
    window.path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    window.path_label.setMaximumHeight(74)
    destination_row.addWidget(window.path_label, 1)
    window.choose_path_btn.setObjectName("chooseDestination")
    window.choose_path_btn.setText("")
    window.choose_path_btn.setIcon(line_icon("folder"))
    window.choose_path_btn.setAccessibleName("選擇下載資料夾")
    window.choose_path_btn.setToolTip("選擇下載資料夾")
    window.choose_path_btn.setFixedWidth(42)
    window.choose_path_btn.setMinimumHeight(42)
    destination_row.addWidget(window.choose_path_btn)
    sidebar.addWidget(destination)
    sidebar.addWidget(_rule())
    sidebar.addWidget(_label("下載紀錄", "field"))
    log = QWidget()
    log_layout = QGridLayout(log)
    log_layout.setContentsMargins(0, 0, 0, 0)
    window.status_box.setObjectName("downloadLog")
    window.status_box.setMinimumHeight(160)
    log_layout.addWidget(window.status_box, 0, 0)
    empty = QWidget()
    empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    empty_layout = QVBoxLayout(empty)
    empty_layout.setContentsMargins(20, 16, 20, 16)
    empty_layout.setSpacing(12)
    empty_layout.addStretch(1)
    tray = QLabel()
    window.log_tray = tray
    tray.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tray.setPixmap(line_icon("tray", 52, "#92A0A8").pixmap(QSize(52, 52)))
    empty_layout.addWidget(tray)
    window.log_empty_label = _label("開始後，會在這裡顯示執行結果。", "muted", wrap=True)
    window.log_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_layout.addWidget(window.log_empty_label)
    empty_layout.addStretch(1)
    log_layout.addWidget(empty, 0, 0)
    window.log_empty_container = empty
    window.status_box.textChanged.connect(lambda: empty.setVisible(window.status_box.document().isEmpty()))
    empty.setVisible(window.status_box.document().isEmpty())
    sidebar.addWidget(log, 1)
    sidebar.addWidget(_rule())
    window.sidebar_scroll.setWidget(summary)
    right_layout.addWidget(window.sidebar_scroll, 1)

    window.advanced_toggle = QToolButton()
    window.advanced_toggle.setObjectName("advancedToggle")
    window.advanced_toggle.setText("進階設定")
    window.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    window.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
    window.advanced_toggle.setCheckable(True)
    window.advanced_toggle.setAccessibleName("展開或收合進階設定")
    right_layout.addWidget(window.advanced_toggle)
    window.advanced_panel = QScrollArea()
    window.advanced_panel.setWidgetResizable(True)
    window.advanced_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.advanced_panel.setMinimumHeight(145)
    window.advanced_panel.setMaximumHeight(245)
    advanced = QWidget()
    advanced_layout = QVBoxLayout(advanced)
    advanced_layout.setContentsMargins(0, 2, 7, 6)
    advanced_layout.setSpacing(11)
    advanced_layout.addWidget(window.folder_by_user_checkbox)
    count = QHBoxLayout()
    window.limit_count_checkbox.setText("限制本輪處理貼文數")
    window.limit_count_checkbox.setToolTip("包含已下載且略過的貼文，避免重跑時超過預期範圍。")
    count.addWidget(window.limit_count_checkbox, 1)
    window.limit_count_input.setFixedWidth(76)
    count.addWidget(window.limit_count_input)
    advanced_layout.addLayout(count)
    date_row = QHBoxLayout()
    window.limit_date_checkbox.setText("只處理此日期起的貼文")
    date_row.addWidget(window.limit_date_checkbox, 1)
    window.limit_date_input.setFixedWidth(168)
    date_row.addWidget(window.limit_date_input)
    advanced_layout.addLayout(date_row)
    advanced_layout.addWidget(window.human_behavior_checkbox)
    advanced_layout.addWidget(window.retry_failed_checkbox)
    advanced_layout.addWidget(_rule())
    window.auto_driver_checkbox.setText("啟動時檢查 ChromeDriver")
    advanced_layout.addWidget(window.auto_driver_checkbox)
    advanced_layout.addWidget(window.driver_check_btn)
    advanced_layout.addWidget(_label("新版下載不需要此驅動；更新失敗不影響收集。", "muted", wrap=True))
    window.advanced_panel.setWidget(advanced)
    window.advanced_panel.hide()

    def toggle_advanced(checked):
        window.advanced_panel.setVisible(checked)
        window.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        tray.setVisible(not checked)

    window.advanced_toggle.toggled.connect(toggle_advanced)
    right_layout.addWidget(window.advanced_panel)
    actions = QHBoxLayout()
    actions.setSpacing(10)
    window.download_btn.setText("開始收集")
    window.download_btn.setProperty("variant", "primary")
    window.download_btn.setIcon(line_icon("download", color="#FFFFFF"))
    window.download_btn.setMinimumHeight(35)
    window.stop_btn.setText("停止")
    window.stop_btn.setIcon(line_icon("stop", color=MUTED))
    window.stop_btn.setMinimumHeight(35)
    actions.addWidget(window.download_btn, 3)
    actions.addWidget(window.stop_btn, 2)
    right_layout.addLayout(actions)
    body.addWidget(right, 2)
    root.addLayout(body, 1)
    # A user-requested, opt-in website link: no background request or popup.
    footer = QHBoxLayout()
    window.promotion_link = _label("", "muted")
    window.promotion_link.setObjectName("promotionLink")
    window.promotion_link.setTextFormat(Qt.TextFormat.RichText)
    window.promotion_link.setTextInteractionFlags(
        Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
    window.promotion_link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    window.promotion_link.setOpenExternalLinks(False)
    window.promotion_link.setToolTip("https://noonisawesome.dev/")
    window.promotion_link.setAccessibleName("試試超讚的歌回直播軟體，開啟官方網站")
    window.promotion_link.linkActivated.connect(
        lambda _url: QDesktopServices.openUrl(QUrl("https://noonisawesome.dev/")))
    footer.addWidget(window.promotion_link)
    footer.addStretch()
    root.addLayout(footer)
    for checkbox in window.findChildren(QCheckBox):
        checkbox.setStyle(window._gallery_control_style)
    for editor in (window.hashtag_input, window.authors_input, window.tweet_urls_input):
        palette = editor.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#586971"))
        editor.setPalette(palette)
    # Enter retains multiline entry; Tab follows the visible workflow.
    window.authors_input.setTabChangesFocus(True)
    window.tweet_urls_input.setTabChangesFocus(True)
    sequence = (
        window.theme_btn, window.help_btn, window.about_btn, window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn,
        window.hashtag_input,
        window.search_checkbox, window.author_media_checkbox,
        window.discover_authors_checkbox, window.authors_input,
        window.tweet_urls_input, window.choose_path_btn,
        window.advanced_toggle, window.folder_by_user_checkbox,
        window.limit_count_checkbox, window.limit_count_input,
        window.limit_date_checkbox, window.limit_date_input,
        window.human_behavior_checkbox, window.retry_failed_checkbox,
        window.auto_driver_checkbox, window.driver_check_btn,
        window.download_btn, window.stop_btn, window.status_box, window.promotion_link,
    )
    for before, after in zip(sequence, sequence[1:]):
        QWidget.setTabOrder(before, after)
    window.hashtag_input.setFocus()
    set_window_theme(window, getattr(window, "theme_mode", "light"))
    refresh_gallery_summary(window)
