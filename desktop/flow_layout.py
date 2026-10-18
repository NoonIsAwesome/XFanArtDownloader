"""Measured native-widget flow shared by tag chips and responsive action groups."""
from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout


class FlowLayout(QLayout):
    # 標籤可縮短顯示；操作按鈕則保留完整 sizeHint，只以自然換行適應寬度。
    # Chips may shrink visually; action buttons retain their full size hint and adapt by wrapping.
    def __init__(self, parent=None, *, shrink_items=True, uniform_height=False):
        super().__init__(parent)
        self._items = []
        self._shrink_items = shrink_items
        self._uniform_height = uniform_height
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

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
        width = max(220, self.minimumSize().width())
        return QSize(width, self.heightForWidth(width))

    def minimumSize(self):
        width = 32 if self._shrink_items else max((item.sizeHint().width() for item in self._items), default=0)
        return QSize(width, max((item.minimumSize().height() for item in self._items), default=0))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, measure=False)

    def _arrange(self, rect, *, measure):
        x, y, row_height = rect.x(), rect.y(), 0
        width = max(1, rect.width())
        shared_height = max((item.sizeHint().height() for item in self._items), default=0)
        for item in self._items:
            hint = item.sizeHint()
            item_width = min(width, hint.width()) if self._shrink_items else hint.width()
            item_height = shared_height if self._uniform_height else hint.height()
            if x > rect.x() and x + item_width > rect.x() + width:
                x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
            if not measure:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_width, item_height)))
            x += item_width + self.spacing()
            row_height = max(row_height, item_height)
        return y - rect.y() + row_height
