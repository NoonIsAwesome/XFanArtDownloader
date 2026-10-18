"""One plain-text chip per target tag, wrapping within the sidebar width."""
from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLabel, QLayout, QSizePolicy, QWidget


class _Chip(QLabel):
    # 長標籤只在畫面省略，提示及無障礙名稱仍保留完整原文。
    # Elide long tags visually while retaining their full literal tooltip and accessible name.
    def __init__(self, value, parent):
        super().__init__(parent)
        self.value = value
        self.setProperty("role", "tagChip")
        self.setProperty("i18nSkip", True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        # QLabel 的自動縮排會吃掉已量好的文字寬度；框線與留白由樣式表負責。
        # Disable QLabel's automatic indent: stylesheet borders/padding already account for the measured width.
        self.setIndent(0)
        self.setToolTip(value)
        self.setAccessibleName(value)
        self._render()

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.value) + 26, max(32, metrics.height() + 14))

    def minimumSizeHint(self):
        return QSize(32, self.sizeHint().height())

    def _render(self):
        self.setText(self.fontMetrics().elidedText(self.value, Qt.TextElideMode.ElideRight,
                                                  max(1, self.width() - 26)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "value"):
            self._render()


class _FlowLayout(QLayout):
    # 使用真正的 Qt 子元件與寬度量測換行；不靠 HTML 或固定欄數模擬標籤。
    # Wrap real Qt child widgets at their measured widths, without HTML or a fixed column count.
    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, max(1, width), 0), measure=True)

    def sizeHint(self):
        return QSize(220, self.heightForWidth(220))

    def minimumSize(self):
        return QSize(32, max((item.minimumSize().height() for item in self._items), default=0))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, measure=False)

    def _arrange(self, rect, *, measure):
        x, y, row_height = rect.x(), rect.y(), 0
        width = max(1, rect.width())
        for item in self._items:
            hint = item.sizeHint()
            item_width = min(width, hint.width())
            if x > rect.x() and x + item_width > rect.x() + width:
                x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
            if not measure:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_width, hint.height())))
            x += item_width + self.spacing()
            row_height = max(row_height, hint.height())
        return y - rect.y() + row_height


class TagChips(QWidget):
    """Retain the summary's text API while giving each tag its own chip."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("i18nSkip", True)
        self.setMinimumWidth(0)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._flow = _FlowLayout(self)
        self._values = ()
        self.chips = []

    def text(self):
        return "  ".join(self._values)

    def setText(self, text):
        self.set_tags((text,) if text else ())

    def set_tags(self, values):
        # 切換語言或主題不重建相同標籤，避免焦點及版面無謂跳動。
        # Keep identical tags during language/theme changes instead of rebuilding their widgets.
        values = tuple(str(value) for value in values)
        if values == self._values:
            return
        while self._flow.count():
            item = self._flow.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self._values = values
        self.chips = [_Chip(value, self) for value in values]
        for chip in self.chips:
            self._flow.addWidget(chip)
            chip.show()
        self.setAccessibleName(self.text())
        self._flow.invalidate()
        self.updateGeometry()
