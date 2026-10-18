"""Keep button focus visible for keyboard navigation without a sticky mouse ring.

Qt correctly restores focus after closing a dialog. A blanket ``:focus`` border
also highlights a button after a mouse click, however. Track the input method
inside this application and expose only its visual state to the stylesheet;
native focus, shortcuts, Tab order and button activation remain unchanged.
"""
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractButton, QApplication


class ButtonFocusVisibility(QObject):
    def __init__(self, application):
        super().__init__(application)
        self.application = application
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
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self.keyboard_navigation = False
            self._set_ring(self.application.focusWidget(), False)
            self._set_ring(watched, False)
        elif kind == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyboard_navigation = True
            self._set_ring(self.application.focusWidget(), True)
        elif kind == QEvent.Type.FocusIn:
            keyboard_reason = event.reason() in (Qt.FocusReason.TabFocusReason,
                                                 Qt.FocusReason.BacktabFocusReason,
                                                 Qt.FocusReason.ShortcutFocusReason)
            self._set_ring(watched, keyboard_reason or (
                self.keyboard_navigation and event.reason() != Qt.FocusReason.MouseFocusReason))
        elif kind == QEvent.Type.FocusOut:
            self._set_ring(watched, False)
        return False


def install_button_focus_visibility():
    """Install once per QApplication, including independently opened dialogs."""
    application = QApplication.instance()
    if application is None:
        return None
    controller = getattr(application, '_fanart_button_focus_visibility', None)
    if controller is None:
        controller = ButtonFocusVisibility(application)
        application.installEventFilter(controller)
        application._fanart_button_focus_visibility = controller
    return controller
