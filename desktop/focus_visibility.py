"""Keep button focus visible for keyboard navigation without a sticky mouse ring.

Qt correctly restores focus after closing a dialog. A blanket ``:focus`` border
also highlights a button after a mouse click, however. Track the input method
inside this application and expose only its visual state to the stylesheet;
native focus, shortcuts, Tab order and button activation remain unchanged.
"""
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractButton, QApplication


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
    def _set_ring(widget, visible):
        if not isinstance(widget, QAbstractButton):
            return
        visible = bool(visible)
        if widget.property('keyboardFocus') is visible:
            return
        widget.setProperty('keyboardFocus', visible)
        # Qt stylesheets need repolishing after a dynamic selector changes.
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def eventFilter(self, watched, event):
        # 依最近的輸入方式區分滑鼠與鍵盤；關閉說明視窗後也能沿用正確的外觀。
        # Track the last input method so returning from a dialog restores the correct mouse or keyboard appearance.
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self.keyboard_navigation = False
            self._set_ring(QApplication.focusWidget(), False)
            self._set_ring(watched, False)
        elif kind == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyboard_navigation = True
            self._set_ring(QApplication.focusWidget(), True)
        elif kind == QEvent.Type.FocusIn:
            keyboard_reason = event.reason() in (Qt.FocusReason.TabFocusReason,
                                                 Qt.FocusReason.BacktabFocusReason,
                                                 Qt.FocusReason.ShortcutFocusReason)
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
