"""Mouse clicks do not leave a ring; keyboard focus and activation still work."""
import os
from pathlib import Path
import sys
import subprocess
import textwrap
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QPushButton, QVBoxLayout, QWidget

from focus_visibility import install_button_focus_visibility
from ui_theme import apply_dialog_theme


class FocusVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = install_button_focus_visibility()
        self.previous_mode = self.controller.keyboard_navigation
        self.controller.keyboard_navigation = False
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.first = QPushButton('Help')
        self.second = QPushButton('About')
        layout.addWidget(self.first)
        layout.addWidget(self.second)
        apply_dialog_theme(self.window, 'dark')
        self.window.show()
        self.window.activateWindow()
        self.app.setActiveWindow(self.window)
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.controller.keyboard_navigation = self.previous_mode

    def test_mouse_dialog_close_restores_native_focus_without_a_highlight_ring(self):
        dialog = QDialog(self.window)
        apply_dialog_theme(dialog, 'dark')
        self.first.clicked.connect(dialog.show)
        QTest.mouseClick(self.first, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(bool(self.first.property('keyboardFocus')))
        dialog.close()
        self.app.setActiveWindow(self.window)
        self.first.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.app.processEvents()
        self.assertTrue(self.first.hasFocus())
        self.assertFalse(bool(self.first.property('keyboardFocus')))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
        self.assertFalse(bool(self.first.property('keyboardFocus')))
        dialog.deleteLater()

    def test_tab_focus_is_visible_and_space_still_activates_the_button(self):
        self.first.setFocus(Qt.FocusReason.MouseFocusReason)
        QTest.keyClick(self.first, Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertTrue(self.second.hasFocus())
        self.assertTrue(self.second.property('keyboardFocus'))
        self.assertFalse(self.first.hasFocus())
        activations = []
        self.second.clicked.connect(lambda: activations.append(True))
        QTest.keyClick(self.second, Qt.Key.Key_Space)
        self.assertEqual(activations, [True])
        self.assertTrue(self.second.property('keyboardFocus'))
        self.second.clearFocus()
        self.assertFalse(self.second.hasFocus())

    def test_focus_out_never_repolishes_a_closing_widget(self):
        # The stylesheet combines the input-method property with :focus. It
        # needs no property mutation while Qt is restoring/destroying focus.
        from unittest.mock import patch
        from PyQt6.QtGui import QFocusEvent
        self.first.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        with patch.object(self.controller, '_set_ring') as set_ring:
            self.controller.eventFilter(
                self.first, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
        set_ring.assert_not_called()

    def test_focus_filter_does_not_delay_application_destruction_until_python_shutdown(self):
        # Qt must release its native widgets while Python/SIP are fully alive.
        # A controller -> application -> controller cycle delays that cleanup
        # until interpreter shutdown and can crash virtual style callbacks.
        code = textwrap.dedent('''
            import gc
            import sys
            import weakref
            sys.path.insert(0, sys.argv[1])
            from PyQt6.QtWidgets import QApplication
            from focus_visibility import install_button_focus_visibility
            def create_application():
                application = QApplication([])
                install_button_focus_visibility()
                return weakref.ref(application)
            gc.disable()
            reference = create_application()
            assert reference() is None, "Focus filter retained QApplication after its owner returned"
        ''')
        result = subprocess.run(
            [sys.executable, '-c', code, str(Path(__file__).resolve().parents[1] / 'desktop')],
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), capture_output=True,
            text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_mouse_click_clears_the_keyboard_ring_without_disabling_focus(self):
        self.controller.keyboard_navigation = True
        self.second.setFocus()
        self.first.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertTrue(self.first.property('keyboardFocus'))
        QTest.mouseClick(self.first, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(self.first.hasFocus())
        self.assertFalse(self.first.property('keyboardFocus'))
        self.assertEqual(self.first.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_theme_application_installs_one_filter_and_keeps_keyboard_focus(self):
        self.second.setFocus()
        self.first.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertTrue(self.first.property('keyboardFocus'))
        apply_dialog_theme(self.window, 'light')
        self.app.processEvents()
        self.assertIs(install_button_focus_visibility(), self.controller)
        self.assertTrue(self.first.property('keyboardFocus'))
