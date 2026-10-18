"""Theme-complete date selection and the existing user notice for the Qt app.

GalleryDateEdit remains a QDateEdit: normal typed dates, date sections, ranges,
and dateChanged signals still belong to Qt. Only its calendar presentation is
replaced. UserNoticeDialog is constructed without running a nested event loop.
"""

from __future__ import annotations

from PyQt6.QtCore import QDate, QEvent, QLocale, QPoint, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPalette, QPen, QTextCharFormat
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QCalendarWidget, QDateEdit, QDialog, QFrame,
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSpinBox,
    QToolButton, QVBoxLayout, QWidget,
)
from i18n import tr, locale as ui_locale


# 共用配色與圖示採延後匯入，避免日期元件和主版面互相載入。
# Import shared colors/icons lazily to avoid circular UI imports.
def _colors(theme: str):
    from ui_design import theme_colors
    return theme_colors(theme)


def _icon(name: str, color: str, size: int = 20):
    from ui_design import line_icon
    return line_icon(name, size=size, color=color)


def _palette(widget: QWidget, colors):
    """Set every relevant role, rather than inheriting the OS dark palette."""
    # 同時覆蓋停用狀態與子元件，避免系統深色背景混入淺色日曆。
    # Cover disabled roles and children too, preventing mixed OS/App palettes.
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["ink"],
        QPalette.ColorRole.Base: colors["input"],
        QPalette.ColorRole.AlternateBase: colors["surface"],
        QPalette.ColorRole.Text: colors["ink"],
        QPalette.ColorRole.Button: colors["surface"],
        QPalette.ColorRole.ButtonText: colors["ink"],
        QPalette.ColorRole.Highlight: colors["accent"],
        QPalette.ColorRole.HighlightedText: colors["accent_ink"],
        QPalette.ColorRole.PlaceholderText: colors["muted"],
        QPalette.ColorRole.ToolTipBase: colors["surface"],
        QPalette.ColorRole.ToolTipText: colors["ink"],
        QPalette.ColorRole.Link: colors["accent"],
    }
    for role, color in roles.items():
        palette.setColor(QPalette.ColorGroup.All, role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled_fg"]))
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled_bg"]))
    widget.setPalette(palette)
    for child in widget.findChildren(QWidget):
        child.setPalette(palette)


def _font(widget: QWidget):
    font = QFont()
    font.setFamilies(["Microsoft JhengHei UI", "Segoe UI"])
    font.setPointSizeF(10)
    widget.setFont(font)


# 保留 Qt 的日期與範圍判斷，只自行繪製選取、今天與相鄰月份的外觀。
# Qt keeps date/range behavior; custom painting handles selection, today, and adjacent months.
class _ThemedCalendar(QCalendarWidget):
    """Paint dates explicitly so weekends and selection cannot retain OS colors."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.colors = None
        self.setLocale(ui_locale())
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.setGridVisible(False)
        self.setMinimumSize(308, 224)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_theme(self, theme: str):
        self.colors = _colors(theme)
        _palette(self, self.colors)
        header = QTextCharFormat()
        header.setForeground(QColor(self.colors["muted"]))
        header.setBackground(QColor(self.colors["surface"]))
        header.setFontWeight(QFont.Weight.DemiBold)
        self.setHeaderTextFormat(header)
        for day in Qt.DayOfWeek:
            if day.value <= 7:
                day_format = QTextCharFormat()
                day_format.setForeground(QColor(self.colors["ink"]))
                self.setWeekdayTextFormat(day, day_format)
        self.updateCells()

    def paintCell(self, painter, rect, date):
        if self.colors is None:
            return super().paintCell(painter, rect, date)
        c = self.colors
        enabled = self.isEnabled() and self.minimumDate() <= date <= self.maximumDate()
        selected = enabled and date == self.selectedDate()
        today = date == QDate.currentDate()
        adjacent = date.month() != self.monthShown() or date.year() != self.yearShown()
        cell = QRectF(rect).adjusted(3, 3, -3, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, QColor(c["input"]))
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QColor(c["accent"]))
            painter.drawRoundedRect(cell, 6, 6)
        elif today and enabled:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(c["accent"]), 1))
            painter.drawRoundedRect(cell, 6, 6)
        text_color = c["accent_ink"] if selected else (
            c["disabled_fg"] if not enabled else c["muted"] if adjacent else c["ink"]
        )
        painter.setPen(QColor(text_color))
        font = QFont(self.font())
        font.setPixelSize(14)
        font.setBold(selected or (today and enabled))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.locale().toString(date.day()))
        painter.restore()


# 獨立的 Popup 提供年／月快速跳轉；關閉時隱藏，下一次仍沿用同一組元件。
# Reuse one popup for year/month jumps; closing hides it rather than rebuilding its widgets.
class _DatePopup(QFrame):
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.owner = owner
        self.setObjectName("galleryDatePopup")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAutoFillBackground(True)
        self.setMinimumWidth(340)
        _font(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.setSpacing(7)
        self.previous_button = QToolButton()
        self.previous_button.setObjectName("calendarPrevious")
        self.previous_button.setAccessibleName("上一個月")
        self.previous_button.setToolTip("上一個月")
        self.previous_button.setFixedSize(32, 32)
        self.next_button = QToolButton()
        self.next_button.setObjectName("calendarNext")
        self.next_button.setAccessibleName("下一個月")
        self.next_button.setToolTip("下一個月")
        self.next_button.setFixedSize(32, 32)
        self.month_button = QToolButton()
        self.month_button.setObjectName("calendarMonth")
        self.month_button.setAccessibleName("選擇月份")
        self.month_button.setToolTip("選擇月份")
        self.month_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.month_menu = QMenu(self.month_button)
        self.month_actions = []
        for month in range(1, 13):
            action = self.month_menu.addAction(ui_locale().standaloneMonthName(month, QLocale.FormatType.LongFormat))
            action.triggered.connect(lambda checked=False, month=month: self._choose_month(month))
            self.month_actions.append(action)
        self.month_button.setMenu(self.month_menu)
        self.month_button.setMinimumWidth(67)
        self.year_input = QSpinBox()
        self.year_input.setObjectName("calendarYear")
        self.year_input.setAccessibleName("年份，可直接輸入")
        self.year_input.setToolTip("直接輸入年份，按 Enter 確認")
        self.year_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.year_input.setKeyboardTracking(False)
        self.year_input.setFixedWidth(90)
        self.year_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.previous_button)
        heading.addStretch(1)
        heading.addWidget(self.year_input)
        heading.addWidget(self.month_button)
        heading.addStretch(1)
        heading.addWidget(self.next_button)
        layout.addLayout(heading)
        self.calendar = _ThemedCalendar()
        layout.addWidget(self.calendar)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.today_button = QPushButton("今天")
        self.today_button.setAccessibleName("選擇今天")
        self.close_button = QPushButton("關閉")
        self.close_button.setAccessibleName("關閉日曆，不更改日期")
        footer.addWidget(self.today_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.previous_button.clicked.connect(lambda: self.calendar.showPreviousMonth())
        self.next_button.clicked.connect(lambda: self.calendar.showNextMonth())
        self.year_input.valueChanged.connect(self._choose_year)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.calendar.clicked.connect(owner._apply_calendar_date)
        self.calendar.activated.connect(owner._apply_calendar_date)
        self.today_button.clicked.connect(lambda: owner._apply_calendar_date(QDate.currentDate()))
        self.close_button.clicked.connect(self.hide)
        self.sync_from_owner()
        self.retranslate_ui()

    def retranslate_ui(self):
        """Locale labels without changing the selected date or browsed month."""
        # 月份名稱交給 QLocale，切換語言不重設使用者正在瀏覽的年月。
        # Let QLocale name months without resetting the currently browsed page.
        year, month = self.calendar.yearShown(), self.calendar.monthShown()
        language = ui_locale()
        for widget in [self, *self.findChildren(QWidget)]:
            widget.setLocale(language)
        self.previous_button.setAccessibleName(tr("上一個月"))
        self.previous_button.setToolTip(tr("上一個月"))
        self.next_button.setAccessibleName(tr("下一個月"))
        self.next_button.setToolTip(tr("下一個月"))
        self.month_button.setAccessibleName(tr("選擇月份"))
        self.month_button.setToolTip(tr("選擇月份"))
        self.year_input.setAccessibleName(tr("年份，可直接輸入"))
        self.year_input.setToolTip(tr("直接輸入年份，按 Enter 確認"))
        self.today_button.setText(tr("今天"))
        self.today_button.setAccessibleName(tr("選擇今天"))
        self.close_button.setText(tr("關閉"))
        self.close_button.setAccessibleName(tr("關閉日曆，不更改日期"))
        for index, action in enumerate(self.month_actions, 1):
            action.setText(language.standaloneMonthName(index, QLocale.FormatType.LongFormat))
        self.calendar.setCurrentPage(year, month)
        self._page_changed(year, month)

    def _choose_month(self, month: int):
        self.calendar.setCurrentPage(self.calendar.yearShown(), month)
        self.calendar.setFocus(Qt.FocusReason.OtherFocusReason)

    def _choose_year(self, year: int):
        # 起訖年可能只允許部分月份，跳年份時也必須遵守原日期欄的範圍。
        # Boundary years may allow only some months; year jumps must honor that range.
        month = self.calendar.monthShown()
        minimum, maximum = self.owner.minimumDate(), self.owner.maximumDate()
        if year == minimum.year():
            month = max(month, minimum.month())
        if year == maximum.year():
            month = min(month, maximum.month())
        self.calendar.setCurrentPage(year, month)

    def _page_changed(self, year: int, month: int):
        # 更新年份顯示時暫停訊號，避免反過來觸發另一輪換頁。
        # Block year signals while synchronizing the header to avoid navigation feedback.
        blocked = self.year_input.blockSignals(True)
        self.year_input.setValue(year)
        self.year_input.blockSignals(blocked)
        self.month_button.setText(ui_locale().standaloneMonthName(month, QLocale.FormatType.ShortFormat))
        first = QDate(year, month, 1)
        last = QDate(year, month, first.daysInMonth())
        minimum, maximum = self.owner.minimumDate(), self.owner.maximumDate()
        self.previous_button.setEnabled(first > QDate(minimum.year(), minimum.month(), 1))
        self.next_button.setEnabled(last < maximum)
        for index, action in enumerate(self.month_actions, start=1):
            month_start = QDate(year, index, 1)
            month_end = QDate(year, index, month_start.daysInMonth())
            action.setEnabled(month_end >= minimum and month_start <= maximum)

    def sync_from_owner(self):
        # 日期欄是資料來源；彈出日曆只同步其選取值、範圍與今天按鈕狀態。
        # The editor owns the date/range; synchronize the calendar and Today availability.
        selected = self.owner.date()
        blocked = self.year_input.blockSignals(True)
        self.year_input.setRange(self.owner.minimumDate().year(), self.owner.maximumDate().year())
        self.year_input.setValue(selected.year())
        self.year_input.blockSignals(blocked)
        self.calendar.setDateRange(self.owner.minimumDate(), self.owner.maximumDate())
        self.calendar.setSelectedDate(selected)
        self.calendar.setCurrentPage(selected.year(), selected.month())
        self.today_button.setEnabled(self.owner.minimumDate() <= QDate.currentDate() <= self.owner.maximumDate())
        self._page_changed(selected.year(), selected.month())

    def set_theme(self, theme: str):
        c = _colors(theme)
        _palette(self, c)
        self.setStyleSheet(f"""
            QFrame#galleryDatePopup {{ background: {c['input']}; color: {c['ink']};
                border: 1px solid {c['border']}; border-radius: 11px; }}
            QToolButton, QPushButton, QSpinBox {{ color: {c['ink']}; background: {c['input']};
                font-size: 14px; border: 1px solid {c['border']}; border-radius: 6px; padding: 5px 8px; }}
            QToolButton {{ min-height: 20px; }}
            QToolButton:hover, QPushButton:hover {{ background: {c['hover']}; border-color: {c['accent']}; }}
            QToolButton:pressed, QPushButton:pressed {{ background: {c['pressed']}; }}
            QToolButton:focus, QPushButton:focus, QSpinBox:focus {{ border-color: {c['accent']}; }}
            QToolButton:disabled, QPushButton:disabled {{ color: {c['disabled_fg']};
                background: {c['disabled_bg']}; border-color: {c['border']}; }}
            QToolButton::menu-indicator {{ image: none; width: 0px; }}
            QSpinBox QLineEdit {{ color: {c['ink']}; background: transparent; border: none; padding: 0px; }}
            QCalendarWidget {{ background: {c['input']}; color: {c['ink']}; border: none; }}
            QCalendarWidget QWidget {{ background: {c['input']}; color: {c['ink']}; }}
            QCalendarWidget QAbstractItemView {{ background: {c['input']}; color: {c['ink']};
                alternate-background-color: {c['surface']}; selection-background-color: {c['accent']};
                selection-color: {c['accent_ink']}; border: none; outline: none; font-size: 14px; }}
            QMenu {{ background: {c['input']}; color: {c['ink']}; border: 1px solid {c['border']}; padding: 4px; }}
            QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['accent_soft_ink']}; }}
            QMenu::item:disabled {{ color: {c['disabled_fg']}; }}
        """)
        self.previous_button.setIcon(_icon("chevron-left", c["ink"], 18))
        self.next_button.setIcon(_icon("chevron-right", c["ink"], 18))
        self.calendar.set_theme(theme)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.owner.setFocus(Qt.FocusReason.OtherFocusReason)
            event.accept()
            return
        super().keyPressEvent(event)


# 仍使用 QDateEdit 處理輸入、鍵盤日期區段與 dateChanged，僅替換日曆入口。
# Keep QDateEdit input, keyboard sections, and dateChanged; replace only calendar presentation.
class GalleryDateEdit(QDateEdit):
    """Editable ISO date with a consistent, theme-complete calendar popup."""

    def __init__(self, parent=None, theme: str = "light"):
        super().__init__(parent)
        self._theme = theme
        self._calendar_popup_enabled = True
        self.setObjectName("galleryDateEdit")
        self.setProperty("i18nSkip", True)
        self.setLocale(ui_locale())
        self.setDisplayFormat("yyyy-MM-dd")
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        super().setCalendarPopup(False)
        self.calendar_button = QToolButton(self)
        self.calendar_button.setObjectName("galleryCalendarButton")
        self.calendar_button.setAccessibleName("開啟日期日曆")
        self.calendar_button.setToolTip("開啟日曆；也可直接輸入 yyyy-MM-dd")
        self.calendar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.calendar_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.calendar_button.setIconSize(QSize(18, 18))
        self.calendar_button.clicked.connect(self.show_calendar_popup)
        self.popup = _DatePopup(self)
        self._popup = self.popup
        self.dateChanged.connect(lambda unused: self.popup.sync_from_owner())
        self.setMinimumHeight(36)
        self.set_theme(theme)
        self.retranslate_ui()

    def retranslate_ui(self):
        selected = self.date()
        section = self.currentSection()
        blocked = self.blockSignals(True)
        self.setLocale(ui_locale())
        self.setDate(selected)
        self.blockSignals(blocked)
        self.setCurrentSection(section)
        self.setAccessibleName(tr("貼文起始日期"))
        self.calendar_button.setAccessibleName(tr("開啟日期日曆"))
        self.calendar_button.setToolTip(tr("開啟日曆；也可直接輸入 yyyy-MM-dd"))
        self.popup.retranslate_ui()

    def calendarPopup(self) -> bool:
        return self._calendar_popup_enabled

    def setCalendarPopup(self, enabled: bool):
        self._calendar_popup_enabled = bool(enabled)
        # 保留原 API 的開關意義，但不啟用會沿用系統外觀的原生日曆箭頭。
        # Preserve the API flag while keeping Qt's native/system-styled popup disabled.
        super().setCalendarPopup(False)
        if hasattr(self, "calendar_button"):
            self.calendar_button.setVisible(bool(enabled))
            if not enabled:
                self.popup.hide()

    def calendarWidget(self) -> QCalendarWidget:
        return self.popup.calendar

    def setMinimumDate(self, minimum: QDate):
        super().setMinimumDate(minimum)
        if hasattr(self, "popup"):
            self.popup.sync_from_owner()

    def setMaximumDate(self, maximum: QDate):
        super().setMaximumDate(maximum)
        if hasattr(self, "popup"):
            self.popup.sync_from_owner()

    def setDateRange(self, minimum: QDate, maximum: QDate):
        super().setDateRange(minimum, maximum)
        if hasattr(self, "popup"):
            self.popup.sync_from_owner()

    def set_theme(self, theme: str):
        self._theme = "dark" if theme == "dark" else "light"
        c = _colors(self._theme)
        _palette(self, c)
        self.setStyleSheet(f"""
            QDateEdit#galleryDateEdit {{ color: {c['ink']}; background: {c['input']};
                border: 1px solid {c['border']}; border-radius: 7px; padding: 7px 36px 7px 9px; font-size: 14px; }}
            QDateEdit#galleryDateEdit:focus {{ border: 2px solid {c['accent']}; padding: 6px 35px 6px 8px; }}
            QDateEdit#galleryDateEdit:disabled {{ color: {c['disabled_fg']}; background: {c['disabled_bg']}; border-color: {c['border']}; }}
            QDateEdit#galleryDateEdit::drop-down, QDateEdit#galleryDateEdit::up-button,
            QDateEdit#galleryDateEdit::down-button {{ width: 0px; border: none; background: transparent; }}
            QDateEdit#galleryDateEdit::down-arrow, QDateEdit#galleryDateEdit::up-arrow {{ image: none; width: 0px; height: 0px; }}
            QDateEdit#galleryDateEdit QLineEdit {{ color: {c['ink']}; background: transparent;
                border: none; border-radius: 0px; padding: 0px; margin: 0px;
                selection-background-color: {c['selection']}; selection-color: {c['accent_soft_ink']}; }}
            QDateEdit#galleryDateEdit QLineEdit:disabled {{ color: {c['disabled_fg']}; }}
            QToolButton#galleryCalendarButton {{ background: transparent; border: none; border-radius: 5px; padding: 0px; }}
            QToolButton#galleryCalendarButton:hover {{ background: {c['hover']}; }}
            QToolButton#galleryCalendarButton:pressed {{ background: {c['pressed']}; }}
            QToolButton#galleryCalendarButton:focus {{ border: 1px solid {c['accent']}; }}
        """)
        self.calendar_button.setIcon(_icon("calendar", c["accent"] if self.isEnabled() else c["disabled_fg"], 18))
        self.popup.set_theme(self._theme)
        self._position_button()
        self.update()

    def _position_button(self):
        if hasattr(self, "calendar_button"):
            self.calendar_button.setGeometry(max(2, self.width() - 33), 3, 29, max(22, self.height() - 6))
            self.calendar_button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_button()

    def changeEvent(self, event):
        super().changeEvent(event)
        # 停用日期限制時，已開啟的日曆也必須關閉，不能留下可操作入口。
        # Disabling the editor must also close its popup so no active date entry remains.
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, "calendar_button"):
            self.calendar_button.setEnabled(self.isEnabled())
            c = _colors(self._theme)
            self.calendar_button.setIcon(_icon("calendar", c["accent"] if self.isEnabled() else c["disabled_fg"], 18))
            if not self.isEnabled():
                self.popup.hide()

    def show_calendar_popup(self):
        if not self.isEnabled() or not self._calendar_popup_enabled:
            return
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.interpretText()
        self.popup.sync_from_owner()
        self.popup.adjustSize()
        # 依所在螢幕的可用範圍定位，避免靠近視窗邊緣時把日曆開到畫面外。
        # Clamp popup placement to its screen's available area near display edges.
        anchor = self.mapToGlobal(QPoint(0, self.height() + 6))
        screen = QGuiApplication.screenAt(anchor) or self.screen()
        available = screen.availableGeometry()
        x = max(available.left() + 4, min(anchor.x(), available.right() - self.popup.width() - 4))
        y = anchor.y()
        if y + self.popup.height() > available.bottom() - 4:
            y = self.mapToGlobal(QPoint(0, 0)).y() - self.popup.height() - 6
        y = max(available.top() + 4, min(y, available.bottom() - self.popup.height() - 4))
        self.popup.move(x, y)
        self.popup.show()
        self.popup.calendar.setFocus(Qt.FocusReason.PopupFocusReason)

    def _apply_calendar_date(self, selected: QDate):
        if self.isEnabled() and self.minimumDate() <= selected <= self.maximumDate():
            self.setDate(selected)
            self.popup.hide()
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F4 or (
            event.key() == Qt.Key.Key_Down and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            self.show_calendar_popup()
            event.accept()
            return
        super().keyPressEvent(event)


# 提醒清單的圓點以 Qt 繪製，避免不同字型的文字符號造成位置或粗細差異。
# Paint notice bullets with Qt to avoid font-dependent symbol alignment and weight.
class _NoticeBullet(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = QColor("#F48472")
        self.setFixedSize(12, 20)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(QPointF(4, 10), 2.5, 2.5)


# 建構子只建立提醒視窗；是否阻擋主流程，由呼叫端決定何時 show／exec。
# Construction only builds the notice; the caller chooses show/exec and its timing.
class UserNoticeDialog(QDialog):
    """The original notice, with readable light and dark themes."""

    def __init__(self, parent=None, theme: str = "light"):
        super().__init__(parent)
        self.setObjectName("userNoticeDialog")
        self.setProperty("i18nSkip", True)
        self.setWindowTitle(tr("使用者提醒"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setModal(True)
        self.setMinimumWidth(490)
        self.resize(540, 430)
        _font(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(20)
        heading = QHBoxLayout()
        heading.setSpacing(14)
        self.notice_icon = QLabel()
        self.notice_icon.setObjectName("noticeIcon")
        self.notice_icon.setFixedSize(46, 46)
        self.notice_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.notice_icon)
        self._translated_labels = []
        self.title_label = self._text("使用者注意事項")
        self.title_label.setObjectName("noticeTitle")
        heading.addWidget(self.title_label, 1)
        layout.addLayout(heading)
        self.introduction_label = self._text(
            "本工具僅供個人非商業用途使用。\n下載的所有圖片與影片著作權均屬原作者所有。"
        )
        self.introduction_label.setWordWrap(True)
        layout.addWidget(self.introduction_label)
        restrictions = QFrame()
        restrictions.setObjectName("noticeRestrictions")
        restriction_layout = QVBoxLayout(restrictions)
        restriction_layout.setContentsMargins(16, 14, 16, 16)
        restriction_layout.setSpacing(10)
        forbidden_label = self._text("請勿：")
        forbidden_label.setObjectName("noticeListHeading")
        restriction_layout.addWidget(forbidden_label)
        self.bullets = []
        for text in (
            "任意將圖片用於商業用途",
            "未經授權轉載、打包公開分享",
            "濫用導致 X 平台封鎖",
        ):
            row = QHBoxLayout()
            row.setSpacing(7)
            bullet = _NoticeBullet()
            self.bullets.append(bullet)
            row.addWidget(bullet, 0, Qt.AlignmentFlag.AlignTop)
            label = self._text(text)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            restriction_layout.addLayout(row)
        layout.addWidget(restrictions)
        self.responsibility_label = self._text("若大量使用造成帳號異常或封鎖，使用者需自行承擔責任。")
        self.responsibility_label.setObjectName("noticeResponsibility")
        self.responsibility_label.setWordWrap(True)
        layout.addWidget(self.responsibility_label)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.accept_button = QPushButton(tr("我知道了"))
        self.accept_button.setObjectName("noticeAccept")
        self.accept_button.setMinimumWidth(112)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)
        footer.addWidget(self.accept_button)
        layout.addLayout(footer)
        self.set_theme(theme)
        self.retranslate_ui()

    def _text(self, source: str) -> QLabel:
        # 保存原文供之後重譯；提醒內容固定以純文字呈現，不當作 HTML。
        # Retain translation source text and render notices as plain text, never HTML.
        label = QLabel(tr(source))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self._translated_labels.append((label, source))
        return label

    def retranslate_ui(self):
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("使用者提醒"))
        for label, source in self._translated_labels:
            label.setText(tr(source))
        self.accept_button.setText(tr("我知道了"))

    def set_theme(self, theme: str):
        self._theme = "dark" if theme == "dark" else "light"
        from ui_design import apply_dialog_theme
        apply_dialog_theme(self, self._theme)
        self.setObjectName("userNoticeDialog")
        c = _colors(self._theme)
        _palette(self, c)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"""
            QDialog#userNoticeDialog {{ background: {c['window']}; color: {c['ink']}; }}
            QLabel {{ color: {c['ink']}; background: transparent; font-size: 14px; }}
            QLabel#noticeTitle {{ font-size: 22px; font-weight: 700; }}
            QLabel#noticeIcon {{ background: {c['accent_soft']}; border-radius: 11px; }}
            QFrame#noticeRestrictions {{ background: {c['surface']}; border: none; border-radius: 11px; }}
            QLabel#noticeListHeading {{ font-size: 15px; font-weight: 600; }}
            QLabel#noticeResponsibility {{ color: {c['muted']}; font-size: 13px; }}
            QPushButton#noticeAccept {{ color: {c['accent_ink']}; background: {c['accent']};
                border: 1px solid {c['accent']}; border-radius: 7px; padding: 9px 18px; font-size: 14px; font-weight: 600; }}
            QPushButton#noticeAccept:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
            QPushButton#noticeAccept:pressed {{ background: {c['accent_pressed']}; border-color: {c['accent_pressed']}; }}
            QPushButton#noticeAccept:focus {{ border: 2px solid {c['ink']}; padding: 8px 17px; }}
        """)
        self.notice_icon.setPixmap(_icon("info", c["accent_soft_ink"], 28).pixmap(QSize(28, 28)))
        for bullet in self.bullets:
            bullet.color = QColor(c["coral"])
            bullet.update()
