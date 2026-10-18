"""Keep control focus visible for keyboard navigation without a sticky mouse ring.

Qt correctly restores focus after closing a dialog. A blanket ``:focus`` border
also highlights controls after a mouse click, however. Track the input method
inside this application and expose only its visual state to the stylesheet;
native focus, shortcuts, Tab order and button activation remain unchanged.
"""
from PyQt6.QtCore import QEvent, QObject, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPalette, QPen
from PyQt6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox,
    QApplication, QComboBox, QLabel, QLineEdit, QPlainTextEdit, QTextEdit, QWidget,
)


class _EditorFrame(QWidget):
    # 只補畫編輯器的外框；子元件不反向保存父視窗，不攔截滑鼠或取得焦點。
    # Paint only the editor frame; retain no parent reference and never intercept input or focus.
    def __init__(self, editor):
        super().__init__(editor)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        editor = self.parentWidget()
        if editor is None:
            return
        keyboard = editor.hasFocus() and bool(editor.property('keyboardFocus'))
        width = 2 if keyboard else 1
        group = QPalette.ColorGroup.Active if editor.isEnabled() else QPalette.ColorGroup.Disabled
        # 子編輯器的樣式表可能重設 palette；沿父層讀取明確保存的主題 token。
        # Editor stylesheets may reset palettes; read explicit theme tokens from the owning ancestor.
        role = QPalette.ColorRole.Link if keyboard else QPalette.ColorRole.Mid
        surface = editor
        token = 'fanartThemeAccent' if keyboard else 'fanartThemeBorder'
        color = None
        while surface is not None:
            color = surface.property(token)
            if color:
                break
            surface = surface.parentWidget()
        color = QColor(color) if color else editor.window().palette().color(group, role)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        offset = width / 2
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(offset, offset, -offset, -offset), 7, 7)
        painter.end()


class ButtonFocusVisibility(QObject):
    # 只控制「是否畫出外框」，保留 Qt 原生焦點、Tab 順序與鍵盤啟動行為。
    # Change only the visual ring while preserving native focus, Tab order and keyboard activation.
    def __init__(self, application):
        super().__init__(application)
        # 重要：不能再反向保存 application，否則會形成循環並延後 Qt 的釋放。
        # Never retain the application back from its owned filter; that cycle delays native Qt teardown.
        # QApplication owns this filter. Do not retain the application back:
        # that Python cycle delays native Qt teardown until interpreter exit,
        # when SIP virtual callbacks may no longer be safe.
        self.keyboard_navigation = False

    @staticmethod
    def _control(widget):
        # 編輯器內部的 lineEdit／viewport 共用外層欄位的框線，不在子元件再畫第二圈。
        # Internal line edits and viewports share their outer control's ring instead of drawing twice.
        if not isinstance(widget, QWidget):
            return None
        parent = widget.parentWidget()
        if isinstance(widget, QLineEdit) and isinstance(parent, (QAbstractSpinBox, QComboBox)):
            return parent
        if isinstance(parent, QAbstractScrollArea) and parent.viewport() is widget:
            return parent
        if isinstance(widget, (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit,
                               QTextEdit, QPlainTextEdit, QAbstractItemView)):
            return widget
        if isinstance(widget, QLabel) and widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
            return widget
        return None

    @classmethod
    def _sync_editor_frame(cls, widget, *, create=False):
        widget = cls._control(widget)
        if not isinstance(widget, (QTextEdit, QPlainTextEdit)):
            return
        frame = getattr(widget, '_fanart_editor_frame', None)
        if frame is None:
            if not create:
                return
            frame = widget._fanart_editor_frame = _EditorFrame(widget)
        frame.setGeometry(widget.rect())
        frame.raise_()
        frame.show()
        frame.update()

    @classmethod
    def _set_ring(cls, widget, visible):
        widget = cls._control(widget)
        if widget is None:
            return
        visible = bool(visible)
        if widget.property('keyboardFocus') is visible:
            cls._sync_editor_frame(widget, create=True)
            return
        widget.setProperty('keyboardFocus', visible)
        # Qt stylesheets need repolishing after a dynamic selector changes.
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()
        cls._sync_editor_frame(widget, create=True)

    def eventFilter(self, watched, event):
        # 依最近的輸入方式區分滑鼠與鍵盤；關閉說明視窗後也能沿用正確的外觀。
        # Track the last input method so returning from a dialog restores the correct mouse or keyboard appearance.
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.PaletteChange):
            self._sync_editor_frame(watched, create=kind == QEvent.Type.Show)
        elif kind == QEvent.Type.FocusOut:
            # 只排程框線重畫；等 Qt 交還焦點後才讀 hasFocus，不在關窗過程重入樣式。
            # Queue painting only; read hasFocus after Qt restores focus without re-entering styles during close.
            frame = getattr(self._control(watched), '_fanart_editor_frame', None)
            if frame is not None:
                frame.update()
        elif kind == QEvent.Type.MouseButtonPress:
            self.keyboard_navigation = False
            self._set_ring(QApplication.focusWidget(), False)
            self._set_ring(watched, False)
        elif kind == QEvent.Type.KeyPress and event.key() not in (
                Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
                Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock):
            self.keyboard_navigation = True
            self._set_ring(QApplication.focusWidget(), True)
        elif kind == QEvent.Type.FocusIn:
            keyboard_reason = event.reason() in (Qt.FocusReason.TabFocusReason,
                                                 Qt.FocusReason.BacktabFocusReason,
                                                 Qt.FocusReason.ShortcutFocusReason)
            # 新視窗可能自行以 Tab 原因選取第一個元件；不能把它當成真實鍵盤輸入。
            # A new window may assign initial Tab focus itself; only real key events change the input mode.
            if event.reason() == Qt.FocusReason.MouseFocusReason:
                self.keyboard_navigation = False
            self._set_ring(watched, keyboard_reason or (
                self.keyboard_navigation and event.reason() != Qt.FocusReason.MouseFocusReason))
        # The selector also requires :focus, so losing focus already hides the
        # ring. Do not repolish during FocusOut: it can run inside a dialog's
        # close/focus-restoration sequence, where re-entering the style is unsafe.
        return False


def install_button_focus_visibility():
    """Install once per QApplication, including independently opened dialogs."""
    # 安裝在應用程式層級，讓主視窗與對話框共用一套規則，且只安裝一次。
    # Install once at application scope so the main window and dialogs share the same focus rules.
    application = QApplication.instance()
    if application is None:
        return None
    controller = getattr(application, '_fanart_button_focus_visibility', None)
    if controller is None:
        controller = ButtonFocusVisibility(application)
        application.installEventFilter(controller)
        application._fanart_button_focus_visibility = controller
    return controller
