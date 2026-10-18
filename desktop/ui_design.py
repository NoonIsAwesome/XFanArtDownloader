"""Bright gallery layout for the existing downloader widgets.

Integration: call ``build_gallery_layout(window)`` from setup_ui. The function
does not replace any input, button, or status_box created by the controller.
``refresh_gallery_summary(window)`` is safe during validation and updates only
tag/source/path summaries. The controller owns run_state_label,
result_detail_label, and login_state_label. Log emptiness follows textChanged.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QProxyStyle, QPushButton,
    QScrollArea, QSizePolicy, QStyle, QStyleOptionButton, QToolButton, QVBoxLayout, QWidget,
)


from ui_theme import apply_dialog_theme, apply_palette, gallery_stylesheet, theme_colors, ui_font
from i18n import tr, language_mode
from resources_rc import get_icon_path
from app_version import APP_VERSION
from tag_chips import TagChips


TEAL = "#176C68"
INK = "#202A30"
MUTED = "#586971"


class WrappingCheckBox(QCheckBox):
    # 保留原生勾選與鍵盤操作，但讓較長的翻譯文字能隨欄寬換行。
    # Keep native checkbox and keyboard behavior while wrapping longer translations to the available width.
    """Preserve native checkbox behavior while letting long translations wrap."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        flags = int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextShowMnemonic)
        bounds = self.fontMetrics().boundingRect(QRect(0, 0, max(24, width - 29), 10000), flags, self.text())
        return max(22, bounds.height() + 4)

    def sizeHint(self):
        return QSize(280, self.heightForWidth(280))

    def minimumSizeHint(self):
        return QSize(80, 22)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_CheckBox, option, painter, self)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(self.palette().color(group, QPalette.ColorRole.WindowText))
        flags = int(Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextShowMnemonic)
        painter.drawText(self.rect().adjusted(29, 0, 0, 0), flags, self.text())
        painter.end()


class HeaderHelpLabel(QLabel):
    """Measure capped header copy at its real width, including live translations."""

    def heightForWidth(self, width):
        # QVBoxLayout can ask about the wider login-button row. The label is
        # capped independently, so measuring that row width loses wrapped lines.
        return super().heightForWidth(max(1, min(width, self.maximumWidth())))

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(hint.width(), max(hint.height(), self.heightForWidth(self.width())))


class ElidedLabel(QLabel):
    # 作者名稱／檔名一律當純文字；畫面放不下時省略，滑鼠提示保留完整內容。
    # Display names and filenames as plain text; elide long labels and retain the full text in a tooltip.
    """One-line public names/filenames; retain literal full text for accessibility."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.literal_text = ""
        self.elide_mode = Qt.TextElideMode.ElideRight
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setProperty("i18nSkip", True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_literal(self, text):
        self.literal_text = text
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self.literal_text, self.elide_mode, max(1, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "literal_text"):
            self._elide()


class PreviewLabel(QLabel):
    # 這裡只縮放已解碼的 QImage；檔案讀取與解碼由 media_preview.py 背景處理。
    # Scale an already decoded QImage here; media_preview.py handles file reads and decoding in the background.
    def __init__(self):
        super().__init__()
        self._image = None
        self._icon = None

    def set_preview(self, image=None, icon=None):
        self._image, self._icon = image, icon
        self._render()

    def _render(self):
        if self._image is not None and not self._image.isNull():
            pixmap = QPixmap.fromImage(self._image).scaled(
                self.contentsRect().size() * 2, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap.setDevicePixelRatio(2)
            self.setPixmap(pixmap)
        elif self._icon is not None:
            self.setPixmap(self._icon.pixmap(42, 42))
        else:
            self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


class GalleryControlStyle(QProxyStyle):
    # 僅自繪勾選記號，其餘狀態與操作仍交由 Qt，保留原生無障礙支援。
    # Customize only the check mark and preserve native Qt interaction and accessibility.
    """Keep native Qt checkbox behavior while drawing a clear teal check mark."""

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
    elif name in ("folder", "folder-open"):
        path = QPainterPath(QPointF(3, 6.5))
        path.lineTo(9, 6.5)
        path.lineTo(11.4, 9)
        path.lineTo(21, 9)
        path.lineTo(21, 19)
        path.lineTo(3, 19)
        path.closeSubpath()
        painter.drawPath(path)
        if name == "folder-open":
            # 展開的前蓋讓「開啟」與「選擇位置」具有不同的圖示。
            # An open front flap distinguishes opening a folder from choosing its location.
            front = QPainterPath(QPointF(3, 10))
            front.lineTo(22, 10)
            front.lineTo(19, 19)
            front.lineTo(3, 19)
            painter.drawPath(front)
        else:
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
    elif name == "more":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        for x in (5, 12, 19):
            painter.drawEllipse(QPointF(x, 12), 1.5, 1.5)
    elif name == "stop":
        painter.drawRoundedRect(QRectF(6, 6, 12, 12), 1.5, 1.5)
    elif name == "video":
        painter.drawRoundedRect(QRectF(3, 5, 13, 14), 2, 2)
        path = QPainterPath(QPointF(16, 9))
        path.lineTo(21, 6)
        path.lineTo(21, 18)
        path.lineTo(16, 15)
        painter.drawPath(path)
    elif name == "image":
        painter.drawRoundedRect(QRectF(3, 3, 18, 18), 2, 2)
        painter.drawEllipse(QRectF(6, 6, 4, 4))
        path = QPainterPath(QPointF(4, 18))
        path.lineTo(10, 12)
        path.lineTo(14, 16)
        path.lineTo(17, 13)
        path.lineTo(20, 16)
        painter.drawPath(path)
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


def _label(text: str, role: str = "body", *, wrap: bool = False, label_type=QLabel) -> QLabel:
    widget = label_type(tr(text))
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
    # 先統一 palette，再套用樣式與圖示；獨立對話框也需要相同的深淺色基準。
    # Apply a consistent palette before styles and icons, including separate dialog windows.
    mode = mode if mode in ("light", "dark") else getattr(window, "theme_mode", "light")
    window.theme_mode = mode
    colors = theme_colors(mode)
    window.setFont(ui_font())
    window.setObjectName("galleryWindow")
    apply_palette(window, mode)
    # Standard Qt dialogs are separate windows. Give them this App's palette
    # too, so an OS dark theme cannot mix with our light text stylesheet.
    app = QApplication.instance()
    if app is not None:
        app.setPalette(window.palette())
    window.setStyleSheet(gallery_stylesheet(mode))
    if hasattr(window, "_gallery_control_style"):
        window._gallery_control_style.colors = colors
        for checkbox in window.findChildren(QCheckBox):
            checkbox.update()
        for spinbox in window.findChildren(QAbstractSpinBox):
            spinbox.update()
    for attr, name, color in (
        ("cookie_btn", "login", "accent_ink"), ("cookie_save_btn", "save", "accent"),
        ("cookie_import_btn", "folder", "accent"),
        ("choose_path_btn", "folder", "accent"), ("download_btn", "download", "accent_ink"),
        ("open_download_folder_btn", "folder-open", "accent"),
        ("stop_btn", "stop", "muted"), ("about_btn", "info", "accent"),
        ("help_btn", "info", "accent"), ("tools_btn", "more", "accent"),
    ):
        if hasattr(window, attr):
            getattr(window, attr).setIcon(line_icon(name, color=colors[color]))
    for attr, icon, size, color in (
        ("log_tray", "tray", 52, "muted"),
        ("strict_info", "info", 20, "accent"),
    ):
        if hasattr(window, attr):
            getattr(window, attr).setPixmap(line_icon(icon, size, colors[color]).pixmap(QSize(size, size)))
    if hasattr(window, "logo_mark"):
        window.logo_mark.setPixmap(QIcon(get_icon_path()).pixmap(48, 48))
    for editor_attr in ("hashtag_input", "authors_input", "tweet_urls_input"):
        if hasattr(window, editor_attr):
            editor = getattr(window, editor_attr)
            palette = editor.palette()
            palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["muted"]))
            editor.setPalette(palette)
    if hasattr(window, "limit_date_input") and hasattr(window.limit_date_input, "set_theme"):
        window.limit_date_input.set_theme(mode)
    if hasattr(window, "theme_btn"):
        window.theme_btn.setText(tr("淺色模式") if mode == "dark" else tr("深色模式"))
        window.theme_btn.setAccessibleName(tr("切換為淺色模式") if mode == "dark" else tr("切換為深色模式"))
        window.theme_btn.setToolTip(tr("目前為深色模式；按一下切換並記住選擇。") if mode == "dark"
                                   else tr("目前為淺色模式；按一下切換並記住選擇。"))
        window.theme_btn.setIcon(line_icon("sun" if mode == "dark" else "moon", color=colors["accent"]))
    if hasattr(window, "promotion_link"):
        window.promotion_link.setText(
            '<a href="https://noonisawesome.dev/" style="color:' + colors["accent"] + '">'
            + tr('試試超讚的歌回直播軟體') + '</a>'
        )
    if hasattr(window, "refresh_current_media"):
        window.refresh_current_media()


def set_window_theme(window, mode):
    apply_gallery_theme(window, mode if mode in ("light", "dark") else "light")


def refresh_gallery_summary(window):
    # 摘要只讀取目前選項並產生說明，實際任務設定仍由 main.snapshot_options 驗證。
    # The summary describes current inputs; main.snapshot_options still validates the actual job.
    """Update summaries without raising while the user is editing a tag."""
    if window.is_downloading and getattr(window, "_active_redownload_count", 0):
        # 歷史重新下載沿用每個項目的原儲存位置，避免摘要誤示目前表單的目錄。
        # History redownloads use each saved destination, not the current form's directory.
        window.tag_summary_label.setText(tr("重新下載選取項目"))
        window.tag_summary_label.setToolTip(window.tag_summary_label.text())
        mode = tr("覆寫原檔") if window._active_redownload_mode == "overwrite" else tr("另存副本")
        window.source_summary_label.setText(tr("重新下載 {count} 個媒體 · {mode}", count=window._active_redownload_count, mode=mode))
        window.path_label.setText(tr("使用各項目原儲存資料夾"))
        window.path_label.setToolTip(window.path_label.text())
        return
    if window.is_downloading and getattr(window, "_active_retry_count", 0):
        window.tag_summary_label.setText(tr("選取媒體重試"))
        window.tag_summary_label.setToolTip(window.tag_summary_label.text())
        window.source_summary_label.setText(tr("僅重試選取的 {count} 個媒體", count=window._active_retry_count))
        window.path_label.setText(str(window.download_path))
        window.path_label.setToolTip(str(window.download_path))
        return
    text = window.hashtag_input.text().strip()
    try:
        from downloader import parse_hashtags
        tags = tuple(parse_hashtags(text))
    except ImportError:
        tags = tuple(dict.fromkeys(piece.lstrip("#") for piece in re.split(r"[\s,，]+", text) if piece.lstrip("#")))
    except (TypeError, ValueError):
        tags = ()
    if tags:
        window.tag_summary_label.set_tags(tuple("#" + tag.lstrip("#") for tag in tags))
    else:
        window.tag_summary_label.setText(tr('請檢查標籤') if text else tr('尚未填寫標籤'))
    window.tag_summary_label.setToolTip(window.tag_summary_label.text())
    sources = []
    if window.search_checkbox.isChecked():
        sources.append(tr('標籤搜尋'))
    if window.author_media_checkbox.isChecked():
        sources.append(tr('作者媒體補抓'))
    if window.tweet_urls_input.toPlainText().strip():
        sources.append(tr('貼文網址'))
    if not sources and window.retry_failed_checkbox.isChecked():
        sources.append(tr('重試未完成'))
    if sources and window.reply_media_checkbox.isChecked():
        sources.append(tr("同串原作者留言"))
    window.source_summary_label.setText("、".join(sources) if sources else tr('請選擇收集來源'))
    window.path_label.setText(str(window.download_path))
    window.path_label.setToolTip(str(window.download_path))


def build_gallery_layout(window):
    """Build approved split workbench A using all controller-owned widgets."""
    apply_gallery_theme(window)
    # 自訂樣式同時有 C++ 物件與 Python 子類別：兩者都必須活得比使用它的元件久。
    # Both the native style and its Python subclass must outlive every widget borrowing the style.
    window._gallery_control_style = GalleryControlStyle("Fusion")
    # QWidget.setStyle() borrows its style. Under a stylesheet Qt also wraps
    # that borrowed base style, so it must survive every borrowing widget.
    # A window parent would delete it before the window's later-added controls.
    application = QApplication.instance()
    window._gallery_control_style.setParent(application)
    # Retain the Python subclass too: Qt can call its virtual painting methods
    # until QApplication has finished releasing all of its widgets.
    if not hasattr(application, "_fanart_control_styles"):
        application._fanart_control_styles = []
    application._fanart_control_styles.append(window._gallery_control_style)
    window.setWindowTitle(f"FanArt Downloader {APP_VERSION}")
    window.setMinimumSize(1020, 740)
    window.resize(1160, 860)
    root = QVBoxLayout(window)
    root.setContentsMargins(26, 22, 26, 22)
    root.setSpacing(20)

    # 頂列：品牌、語言／主題／說明，以及登入操作；和下方收集表單分開管理。
    # Keep branding, language, theme, help and login controls separate from the collection form.
    header = QHBoxLayout()
    header.setSpacing(14)
    brand_mark = QLabel()
    window.logo_mark = brand_mark
    brand_mark.setPixmap(QIcon(get_icon_path()).pixmap(48, 48))
    brand_mark.setFixedSize(48, 48)
    header.addWidget(brand_mark)
    brand = QVBoxLayout()
    brand.setSpacing(3)
    brand.addStretch(1)
    brand.addWidget(_label("FanArt Downloader", "brand"))
    brand.addWidget(_label("收集帶有指定標籤的作品", "muted"))
    brand.addStretch(1)
    header.addLayout(brand)
    header.addStretch(1)
    auth = QVBoxLayout()
    auth.setSpacing(6)
    auth_buttons = QHBoxLayout()
    auth_buttons.setSpacing(9)
    window.cookie_btn.setText(tr('開啟瀏覽器登入'))
    window.cookie_btn.setIcon(line_icon("login", color="#FFFFFF"))
    window.cookie_btn.setProperty("variant", "primary")
    window.cookie_save_btn.setText(tr('儲存登入狀態'))
    window.cookie_save_btn.setIcon(line_icon("save"))
    window.cookie_btn.setToolTip(tr('以一般 Chrome／Edge 模式開啟本工具專用視窗；請在網站中輸入帳號與密碼。'))
    window.cookie_save_btn.setToolTip(tr('先關閉專用登入視窗，再讀取同一個本機 profile 的 X 登入狀態。重新開啟 App 後也能直接儲存。'))
    window.cookie_import_btn.setText(tr('匯入 Cookie 檔'))
    window.cookie_import_btn.setIcon(line_icon("folder"))
    window.cookie_import_btn.setToolTip(tr('選擇 Netscape Cookie TXT 或舊版 cookies.json；與登入後儲存是不同操作。'))
    window.theme_btn = QPushButton(tr('深色模式'))
    window.about_btn = QPushButton(tr('作者資訊'))
    window.help_btn = QPushButton(tr('使用說明'))
    window.help_btn.setAccessibleName(tr('開啟使用說明與登入引導'))
    # 語言欄有固定可見標籤，避免只看「跟隨電腦設定」時不清楚用途。
    # A persistent language label explains the control even when the selected value is Follow system.
    utilities = QHBoxLayout()
    utilities.setSpacing(9)
    utilities.addStretch(1)
    window.language_combo = QComboBox()
    window.language_combo.setObjectName("languageCombo")
    window.language_combo.setProperty("i18nSkip", True)
    window.language_combo.setMinimumWidth(156)
    window.language_combo.setMaximumWidth(200)
    window.language_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    for title, code in ((tr("跟隨電腦設定"), "system"), ("繁體中文", "zh_TW"), ("简体中文", "zh_CN"),
                        ("English", "en"), ("日本語", "ja"), ("한국어", "ko")):
        window.language_combo.addItem(title, code)
    window.language_combo.setCurrentIndex(max(0, window.language_combo.findData(language_mode())))
    window.language_combo.setAccessibleName(tr("介面語言"))
    window.language_combo.setToolTip(tr("選擇介面語言；系統選項會跟隨電腦設定。"))
    window.language_combo.setStyle(window._gallery_control_style)
    window.language_label = _label("介面語言", "muted")
    window.language_label.setBuddy(window.language_combo)
    utilities.addWidget(window.language_label)
    utilities.addWidget(window.language_combo)
    utilities.addWidget(window.theme_btn)
    utilities.addWidget(window.help_btn)
    utilities.addWidget(window.about_btn)
    # 次要功能收進單一工具選單，避免多語系文字把頂列擠出視窗。
    # Group secondary actions in one tools menu to keep translated headers within the window.
    window.tools_btn = QPushButton(tr("更多功能"))
    window.tools_btn.setIcon(line_icon("more", color=TEAL))
    window.tools_menu = QMenu(window.tools_btn)
    window.history_action = window.tools_menu.addAction(tr("下載紀錄與收集進度"))
    window.update_action = window.tools_menu.addAction(tr("檢查 App 與擷取工具更新"))
    window.tools_menu.addSeparator()
    window.clear_login_action = window.tools_menu.addAction(tr("清除本機登入資料"))
    window.tools_btn.setMenu(window.tools_menu)
    auth.addLayout(utilities)
    auth_buttons.addWidget(window.cookie_btn)
    auth_buttons.addWidget(window.cookie_save_btn)
    auth_buttons.addWidget(window.cookie_import_btn)
    auth_buttons.addWidget(window.tools_btn)
    auth.addLayout(auth_buttons)
    window.login_state_label = _label("尚未儲存登入資料", "muted")
    window.login_state_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    window.login_state_label.setWordWrap(True)
    window.login_state_label.setToolTip(tr('本機已儲存 Cookie 不代表已驗證 X 伺服器登入是否仍有效。'))
    auth.addWidget(window.login_state_label)
    window.login_help_label = _label(
        "登入完成後，請關閉剛才的專用瀏覽器視窗，再回來按儲存。\n"
        "專用瀏覽器設定檔與 X Cookie 只保留在本機 data，請勿分享。", "muted", wrap=True,
        label_type=HeaderHelpLabel,
    )
    window.login_help_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    window.login_help_label.setMaximumWidth(530)
    help_policy = window.login_help_label.sizePolicy()
    help_policy.setHeightForWidth(True)
    help_policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
    window.login_help_label.setSizePolicy(help_policy)
    auth.addWidget(window.login_help_label)
    header.addLayout(auth)
    root.addLayout(header)
    root.addWidget(_rule())

    # 主區域採雙欄；左邊輸入來源，右邊保留本次狀態、紀錄與操作按鈕。
    # Use the left column for sources and the right column for progress, logs and job actions.
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
    # 標籤是所有原貼文的共通篩選條件，多個標籤採任一完整匹配。
    # Every original post must match at least one complete requested tag.
    tags = QVBoxLayout()
    tags.setSpacing(7)
    tag_label = _label("目標標籤", "field")
    tag_label.setBuddy(window.hashtag_input)
    tags.addWidget(tag_label)
    window.hashtag_input.setMinimumHeight(27)
    window.hashtag_input.setPlaceholderText(tr('#角色名稱 #作品標籤'))
    window.hashtag_input.setAccessibleName(tr('目標標籤，可填多個，以空格或逗號分隔'))
    tags.addWidget(window.hashtag_input)
    tags.addWidget(_label("多個標籤以空格或逗號分隔；符合任一完整標籤即收集，重複標籤會合併。", "muted", wrap=True))
    form.addLayout(tags)

    search = QFrame()
    search.setObjectName("searchSurface")
    search_row = QHBoxLayout(search)
    search_row.setContentsMargins(16, 13, 16, 13)
    search_row.setSpacing(16)
    window.search_checkbox.setText(tr('標籤搜尋'))
    window.search_checkbox.setProperty("role", "source")
    search_row.addWidget(window.search_checkbox)
    search_row.addWidget(_label("搜尋含指定標籤的貼文。", "muted", wrap=True), 1)
    form.addWidget(search)

    # 作者補抓分成自動發現與手動補充，手動帳號只是選填，不是開始前的必要條件。
    # Separate automatically discovered authors from optional manual additions; a manual handle is not required.
    authors = QFrame()
    authors.setObjectName("authorSurface")
    author_layout = QVBoxLayout(authors)
    author_layout.setContentsMargins(16, 14, 16, 16)
    author_layout.setSpacing(9)
    window.author_media_checkbox.setText(tr('作者媒體補抓'))
    window.author_media_checkbox.setProperty("role", "source")
    author_layout.addWidget(window.author_media_checkbox)
    auto = QVBoxLayout()
    auto.setContentsMargins(26, 0, 0, 0)
    auto.setSpacing(5)
    window.discover_authors_checkbox.setText(tr('自動補抓已發現的作者'))
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
    window.authors_input.setPlaceholderText(tr('@作者帳號\n或 https://x.com/作者/media'))
    author_layout.addWidget(window.authors_input)
    form.addWidget(authors)

    # 貼網址下載是獨立入口；仍使用同一套後端篩選、命名與下載紀錄。
    # Direct post URLs share the same backend filtering, naming and download history.
    urls = QVBoxLayout()
    urls.setSpacing(6)
    url_label = _label("貼文網址（選填）", "field")
    url_label.setBuddy(window.tweet_urls_input)
    urls.addWidget(url_label)
    window.tweet_urls_input.setMinimumHeight(56)
    window.tweet_urls_input.setMaximumHeight(72)
    window.tweet_urls_input.setPlaceholderText(tr('https://x.com/作者/status/貼文編號\n每行一則貼文網址'))
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
    strict_row.addWidget(_label(
        "搜尋、作者媒體頁與貼文網址皆核對原貼文的完整標籤；若啟用同串補抓，僅該原貼文作者的留言可不含標籤。",
        "body", wrap=True,
    ), 1)
    form.addWidget(strict)
    form.addStretch(1)
    window.collection_scroll.setWidget(left)
    body.addWidget(window.collection_scroll, 3)

    divider = QFrame()
    divider.setFixedWidth(1)
    divider.setObjectName("galleryRule")
    body.addWidget(divider)
    # 右欄內容可捲動，進階選項和開始／停止按鈕另外保留位置。
    # Let sidebar content scroll while keeping advanced options and job actions in their own space.
    right = QWidget()
    right.setMinimumWidth(390)
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
    window.tag_summary_label = TagChips()
    window.tag_summary_label.setProperty("i18nSkip", True)
    window.tag_summary_label.setObjectName("tagSummary")
    sidebar.addWidget(window.tag_summary_label)
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
    window.path_label.setProperty("i18nSkip", True)
    window.path_label.setWordWrap(True)
    window.path_label.setMinimumWidth(0)
    window.path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    window.path_label.setMaximumHeight(74)
    destination_row.addWidget(window.path_label, 1)
    window.choose_path_btn.setObjectName("chooseDestination")
    window.choose_path_btn.setText("")
    window.choose_path_btn.setIcon(line_icon("folder"))
    window.choose_path_btn.setAccessibleName(tr('選擇下載資料夾'))
    window.choose_path_btn.setToolTip(tr('選擇下載資料夾'))
    window.choose_path_btn.setFixedWidth(42)
    window.choose_path_btn.setMinimumHeight(42)
    destination_row.addWidget(window.choose_path_btn)
    # 明確區分選擇位置與開啟位置；一般下載進行中仍可查看資料夾。
    # Separate choosing the destination from opening it; normal downloads can keep running.
    window.open_download_folder_btn = QPushButton(tr("開啟"))
    window.open_download_folder_btn.setIcon(line_icon("folder-open"))
    window.open_download_folder_btn.setAccessibleName(tr("開啟下載資料夾"))
    window.open_download_folder_btn.setToolTip(tr("開啟下載資料夾"))
    window.open_download_folder_btn.setMinimumHeight(42)
    destination_row.addWidget(window.open_download_folder_btn)
    sidebar.addWidget(destination)
    sidebar.addWidget(_rule())
    # 紀錄與預覽直接可見，不再只藏在頂端的更多選單。
    # Keep history and previews discoverable instead of placing the entry only in the overflow menu.
    history_heading = QHBoxLayout()
    history_heading.addWidget(_label("下載紀錄", "field"), 1)
    window.history_button = QPushButton(tr("查看紀錄與預覽"))
    window.history_button.setAccessibleName(tr("查看紀錄與預覽"))
    window.history_button.setToolTip(tr("瀏覽已下載項目、作者資訊與圖片預覽"))
    history_heading.addWidget(window.history_button)
    sidebar.addLayout(history_heading)
    window.current_media_row = QWidget()
    # 公開頭像與作者文字只顯示當前下載項目，不會使用登入 Cookie 去抓頭像。
    # Show the current author using a public avatar request without login cookies.
    current_row = QHBoxLayout(window.current_media_row)
    current_row.setContentsMargins(0, 0, 0, 0)
    current_row.setSpacing(10)
    window.current_avatar_label = QLabel()
    window.current_avatar_label.setFixedSize(40, 40)
    window.current_avatar_label.setAccessibleName(tr("目前作者的頭像"))
    current_row.addWidget(window.current_avatar_label)
    current_text = QVBoxLayout()
    current_text.setSpacing(2)
    window.current_author_label = ElidedLabel()
    window.current_file_label = ElidedLabel()
    window.current_file_label.elide_mode = Qt.TextElideMode.ElideMiddle
    window.current_file_label.setProperty("role", "muted")
    current_text.addWidget(window.current_author_label)
    current_text.addWidget(window.current_file_label)
    current_row.addLayout(current_text, 1)
    window.current_media_row.hide()
    sidebar.addWidget(window.current_media_row)
    # 空白狀態與實際紀錄共用位置，首次開啟時也能看懂下一步要做什麼。
    # Share the same area for the empty-state guidance and the active download log.
    log = QWidget()
    log_layout = QGridLayout(log)
    log_layout.setContentsMargins(0, 0, 0, 0)
    window.status_box.setObjectName("downloadLog")
    window.status_box.setMinimumHeight(100)
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
    window.advanced_toggle.setText(tr('進階設定'))
    window.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    window.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
    window.advanced_toggle.setCheckable(True)
    window.advanced_toggle.setAccessibleName(tr('展開或收合進階設定'))
    right_layout.addWidget(window.advanced_toggle)
    window.advanced_panel = QScrollArea()
    window.advanced_panel.setWidgetResizable(True)
    window.advanced_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.advanced_panel.setMinimumHeight(145)
    window.advanced_panel.setMaximumHeight(245)
    # 進階功能集中折疊；搜尋上限僅限制搜尋階段，作者與同串補抓仍會繼續。
    # Group advanced controls; the search quota does not stop author or same-thread backfill.
    advanced = QWidget()
    advanced_layout = QVBoxLayout(advanced)
    advanced_layout.setContentsMargins(0, 2, 7, 6)
    advanced_layout.setSpacing(11)
    advanced_layout.addWidget(window.folder_by_user_checkbox)
    count = QHBoxLayout()
    window.limit_count_checkbox.setText(tr("搜尋貼文上限"))
    window.limit_count_checkbox.setToolTip(tr("只計算標籤搜尋符合條件的不同原貼文（含已下載）；作者、貼文網址、重試與同串補抓不受此上限限制。"))
    count.addWidget(window.limit_count_checkbox, 1)
    window.limit_count_input.setFixedWidth(76)
    count.addWidget(window.limit_count_input)
    advanced_layout.addLayout(count)
    advanced_layout.addWidget(_label("搜尋達上限後仍會進行作者補抓；本輪總數可能超過上限。", "muted", wrap=True))
    date_row = QHBoxLayout()
    window.limit_date_checkbox.setText(tr('只處理此日期起的貼文'))
    date_row.addWidget(window.limit_date_checkbox, 1)
    window.limit_date_input.setFixedWidth(168)
    date_row.addWidget(window.limit_date_input)
    advanced_layout.addLayout(date_row)
    advanced_layout.addWidget(window.date_window_search_checkbox)
    advanced_layout.addWidget(window.resume_checkbox)
    advanced_layout.addWidget(_label(
        "分段搜尋需設定起始日期；接續工作會略過已完成來源，未完成段落可能重新掃描。",
        "muted", wrap=True,
    ))
    advanced_layout.addWidget(window.human_behavior_checkbox)
    advanced_layout.addWidget(window.retry_failed_checkbox)
    advanced_layout.addWidget(_rule())
    advanced_layout.addWidget(window.reply_media_checkbox)
    advanced_layout.addWidget(_label(
        "原貼文符合目標標籤才補抓；留言可不含標籤，只收同一作者，不下載其他人的留言附件。",
        "muted", wrap=True,
    ))
    advanced_layout.addWidget(_label("X 可見範圍有限，討論串補抓不保證全量。", "muted", wrap=True))
    advanced_layout.addWidget(_rule())
    advanced_layout.addWidget(_label("連線方式", "field"))
    advanced_layout.addWidget(_label(
        "X 載入或下載較慢時，可選用 WARP 本機代理；一般連線正常時不需要開啟。",
        "muted", wrap=True,
    ))
    advanced_layout.addWidget(window.warp_proxy_checkbox)
    # WARP 是選用的本機 SOCKS5 端點；這個畫面不負責開啟 VPN 或修改系統網路。
    # WARP is an optional local SOCKS5 endpoint; this view never enables a VPN or changes system networking.
    proxy_row = QHBoxLayout()
    proxy_label = _label("本機代理連接埠")
    proxy_label.setBuddy(window.warp_proxy_port_input)
    proxy_row.addWidget(proxy_label, 1)
    window.warp_proxy_port_input.setFixedWidth(112)
    proxy_row.addWidget(window.warp_proxy_port_input)
    advanced_layout.addLayout(proxy_row)
    advanced_layout.addWidget(_label(
        "需先在官方 WARP 啟用 Local proxy；僅搜尋、補抓與下載使用代理，登入視窗沿用電腦設定。代理失敗時不會改用直連。",
        "muted", wrap=True,
    ))
    advanced_layout.addWidget(_rule())
    window.auto_driver_checkbox.setText(tr('啟動時檢查 ChromeDriver'))
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
    # 操作按鈕固定在捲動內容之外，下載中仍能直接停止。
    # Keep job actions outside the scrolling content so Stop remains directly accessible.
    actions = QHBoxLayout()
    actions.setSpacing(10)
    window.download_btn.setText(tr('開始收集'))
    window.download_btn.setProperty("variant", "primary")
    window.download_btn.setIcon(line_icon("download", color="#FFFFFF"))
    window.download_btn.setMinimumHeight(35)
    window.stop_btn.setText(tr('停止'))
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
    window.promotion_link.setAccessibleName(tr('試試超讚的歌回直播軟體，開啟官方網站'))
    window.promotion_link.linkActivated.connect(
        lambda _url: QDesktopServices.openUrl(QUrl("https://noonisawesome.dev/")))
    footer.addWidget(window.promotion_link)
    footer.addStretch()
    root.addLayout(footer)
    for checkbox in window.findChildren(QCheckBox):
        checkbox.setStyle(window._gallery_control_style)
    for spinbox in window.findChildren(QAbstractSpinBox):
        spinbox.setStyle(window._gallery_control_style)
    for editor in (window.hashtag_input, window.authors_input, window.tweet_urls_input):
        palette = editor.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#586971"))
        editor.setPalette(palette)
    # Enter retains multiline entry; Tab follows the visible workflow.
    window.authors_input.setTabChangesFocus(True)
    window.tweet_urls_input.setTabChangesFocus(True)
    sequence = (
        window.language_combo, window.theme_btn, window.help_btn, window.about_btn, window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn, window.tools_btn,
        window.hashtag_input,
        window.search_checkbox, window.author_media_checkbox,
        window.discover_authors_checkbox, window.authors_input,
        window.tweet_urls_input, window.choose_path_btn,
        window.advanced_toggle, window.folder_by_user_checkbox,
        window.limit_count_checkbox, window.limit_count_input,
        window.limit_date_checkbox, window.limit_date_input,
        window.date_window_search_checkbox, window.resume_checkbox,
        window.human_behavior_checkbox, window.retry_failed_checkbox,
        window.reply_media_checkbox,
        window.warp_proxy_checkbox, window.warp_proxy_port_input,
        window.auto_driver_checkbox, window.driver_check_btn,
        window.download_btn, window.stop_btn, window.status_box, window.promotion_link,
    )
    for before, after in zip(sequence, sequence[1:]):
        QWidget.setTabOrder(before, after)
    window.hashtag_input.setFocus()
    set_window_theme(window, getattr(window, "theme_mode", "light"))
    refresh_gallery_summary(window)
