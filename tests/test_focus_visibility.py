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
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QRadioButton, QSpinBox, QStyle, QStyleOptionSpinBox, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

from focus_visibility import install_button_focus_visibility
from ui_theme import apply_dialog_theme, theme_colors


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
            from PyQt6.QtCore import QEvent
            from PyQt6.QtWidgets import QApplication, QTextEdit
            from focus_visibility import install_button_focus_visibility
            from ui_theme import apply_dialog_theme
            def create_application():
                application = QApplication([])
                install_button_focus_visibility()
                editor = QTextEdit('Offline fixture')
                apply_dialog_theme(editor, 'light')
                editor.show()
                application.processEvents()
                assert editor._fanart_editor_frame.parentWidget() is editor
                editor.close()
                editor.deleteLater()
                application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
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

    def add_control(self, control):
        self.window.layout().addWidget(control)
        self.window.adjustSize()
        self.app.processEvents()
        return control

    def test_inputs_and_choice_controls_keep_native_focus_without_mouse_rings(self):
        controls = [QLineEdit('Tag'), QTextEdit('URLs'), QPlainTextEdit('Authors'),
                    QComboBox(), QSpinBox(), QCheckBox('Search'), QRadioButton('Copy'), QToolButton()]
        controls[3].addItems(['English', '日本語'])
        for control in controls:
            self.add_control(control)
            with self.subTest(control=type(control).__name__):
                self.first.setFocus(Qt.FocusReason.MouseFocusReason)
                control.setFocus(Qt.FocusReason.TabFocusReason)
                self.app.processEvents()
                self.assertTrue(control.hasFocus())
                self.assertTrue(control.property('keyboardFocus'))
                target = control.viewport() if isinstance(control, (QTextEdit, QPlainTextEdit)) else control
                QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
                self.app.processEvents()
                self.assertFalse(bool(control.property('keyboardFocus')))
                self.assertNotEqual(control.focusPolicy(), Qt.FocusPolicy.NoFocus)
                if isinstance(control, QComboBox):
                    control.hidePopup()

    def test_spinbox_line_edit_and_text_viewport_use_only_the_outer_ring(self):
        spin = self.add_control(QSpinBox())
        text = self.add_control(QTextEdit())
        for outer, inner in ((spin, spin.lineEdit()), (text, text.viewport())):
            with self.subTest(control=type(outer).__name__):
                self.first.setFocus(Qt.FocusReason.MouseFocusReason)
                inner.setFocus(Qt.FocusReason.TabFocusReason)
                self.app.processEvents()
                self.assertIs(self.controller._control(inner), outer)
                self.assertTrue(outer.property('keyboardFocus'))
                self.assertFalse(bool(inner.property('keyboardFocus')))
                QTest.mouseClick(inner, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
                self.assertFalse(bool(outer.property('keyboardFocus')))

    def test_combo_popup_restores_mouse_or_keyboard_appearance_across_themes(self):
        combo = self.add_control(QComboBox())
        combo.addItems(['English', '日本語'])
        for theme in ('light', 'dark'):
            apply_dialog_theme(self.window, theme)
            self.first.setFocus(Qt.FocusReason.MouseFocusReason)
            combo.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(combo, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
            self.app.processEvents()
            combo.hidePopup()
            combo.setFocus(Qt.FocusReason.PopupFocusReason)
            self.app.processEvents()
            self.assertTrue(combo.property('keyboardFocus'))
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            combo.hidePopup()
            combo.setFocus(Qt.FocusReason.PopupFocusReason)
            self.app.processEvents()
            self.assertTrue(combo.hasFocus())
            self.assertFalse(bool(combo.property('keyboardFocus')))
            apply_dialog_theme(self.window, 'dark' if theme == 'light' else 'light')
            self.assertFalse(bool(combo.property('keyboardFocus')))

    def test_editor_border_changes_only_for_keyboard_and_typing_is_preserved(self):
        editor = self.add_control(QLineEdit('40000'))
        for theme in ('light', 'dark'):
            apply_dialog_theme(self.window, theme)
            self.first.setFocus(Qt.FocusReason.MouseFocusReason)
            editor.setFocus(Qt.FocusReason.TabFocusReason)
            self.app.processEvents()
            image = editor.grab().toImage()
            self.assertEqual(image.pixelColor(1, image.height() // 2), QColor(theme_colors(theme)['accent']))
            QTest.mouseClick(editor, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            image = editor.grab().toImage()
            self.assertNotEqual(image.pixelColor(1, image.height() // 2), QColor(theme_colors(theme)['accent']))
            editor.selectAll()
            QTest.keyClicks(editor, '50000')
            self.assertEqual(editor.text(), '50000')
            self.assertTrue(editor.property('keyboardFocus'))

    def test_editable_combo_and_keyboard_links_share_input_method_rules(self):
        combo = self.add_control(QComboBox())
        combo.setEditable(True)
        label = self.add_control(QLabel('<a href="https://example.invalid">Website</a>'))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByKeyboard | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for control, target in ((combo, combo.lineEdit()), (label, label)):
            self.first.setFocus(Qt.FocusReason.MouseFocusReason)
            target.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.app.processEvents()
            self.assertTrue(control.property('keyboardFocus'))
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
            self.assertFalse(bool(control.property('keyboardFocus')))

    def test_numeric_buttons_are_wide_and_keep_native_step_limits(self):
        from ui_design import GalleryControlStyle
        from gallery_widgets import GallerySpinBox
        spin = self.add_control(GallerySpinBox())
        spin.setRange(1, 65535)
        spin.setValue(40000)
        spin.resize(160, 42)
        # 樣式與實際程式一樣由 QApplication 持有，不能在元件前先釋放 Python 子類別。
        # Match production ownership so the Python style outlives every borrowing widget.
        style = GalleryControlStyle('Fusion')
        style.setParent(self.app)
        if not hasattr(self.app, '_fanart_control_styles'):
            self.app._fanart_control_styles = []
        self.app._fanart_control_styles.append(style)
        spin.setStyle(style)
        for theme in ('light', 'dark'):
            style.colors = theme_colors(theme)
            apply_dialog_theme(self.window, theme)
            option = QStyleOptionSpinBox()
            spin.initStyleOption(option)
            up = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxUp, spin)
            down = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxDown, spin)
            self.assertGreaterEqual(up.width(), 28)
            self.assertGreaterEqual(down.width(), 28)
            self.assertFalse(up.intersects(down))
            spin.setValue(40000)
            self.app.processEvents()
            # 實際圖素必須包含箭頭，不能只驗證有可點擊但空白的區域。
            # Check painted pixels too: a wide clickable but blank button is insufficient.
            image = spin.grab().toImage()
            ink = QColor(theme_colors(theme)['ink'])
            for button in (up, down):
                center = button.center()
                self.assertTrue(any(image.pixelColor(x, y) == ink
                    for x in range(center.x()-4, center.x()+5) for y in range(center.y()-3, center.y()+4)))
            QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up.center())
            self.assertEqual(spin.value(), 40001)
            self.assertFalse(bool(spin.property('keyboardFocus')))
            QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=down.center())
            self.assertEqual(spin.value(), 40000)
            spin.setValue(spin.maximum())
            QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up.center())
            self.assertEqual(spin.value(), 65535)
            spin.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(spin, Qt.Key.Key_Down)
            self.assertEqual(spin.value(), 65534)
            self.assertTrue(spin.property('keyboardFocus'))

    def test_multiline_editor_frames_follow_focus_resize_theme_and_mouse_scroll(self):
        for editor in (QTextEdit(), QPlainTextEdit()):
            editor.setPlainText('\n'.join(f'Offline row {number}' for number in range(80)))
            editor.setFixedHeight(100)
            self.add_control(editor)
            for theme in ('light', 'dark'):
                with self.subTest(editor=type(editor).__name__, theme=theme):
                    apply_dialog_theme(self.window, theme)
                    self.first.setFocus(Qt.FocusReason.MouseFocusReason)
                    editor.setFocus(Qt.FocusReason.TabFocusReason)
                    self.app.processEvents()
                    image = editor.grab().toImage()
                    self.assertEqual(image.pixelColor(1, image.height() // 2).name(), theme_colors(theme)['accent'].lower())
                    frame = editor._fanart_editor_frame
                    self.assertIs(frame.parentWidget(), editor)
                    self.assertTrue(frame.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
                    self.assertEqual(frame.focusPolicy(), Qt.FocusPolicy.NoFocus)
                    self.window.resize(self.window.width()+20, self.window.height())
                    self.app.processEvents()
                    self.assertEqual(frame.geometry(), editor.rect())
                    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
                    self.app.processEvents()
                    image = editor.grab().toImage()
                    self.assertNotEqual(image.pixelColor(1, image.height() // 2), QColor(theme_colors(theme)['accent']))
                    self.first.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.app.processEvents()
                    image = editor.grab().toImage()
                    self.assertNotEqual(image.pixelColor(1, image.height() // 2), QColor(theme_colors(theme)['accent']))
                    bar = editor.verticalScrollBar()
                    bar.setValue(0)
                    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(bar.width()//2, bar.height()-3))
                    self.assertGreater(bar.value(), 0)

    def test_date_popup_and_specific_button_rules_preserve_keyboard_visibility(self):
        from gallery_widgets import GalleryDateEdit
        from ui_theme import gallery_stylesheet
        date = self.add_control(GalleryDateEdit(self.window))
        destination = self.add_control(QPushButton('Folder'))
        destination.setObjectName('chooseDestination')
        for theme in ('light', 'dark'):
            self.window.setStyleSheet(gallery_stylesheet(theme))
            date.set_theme(theme)
            for control in (date, date.calendar_button, destination):
                self.first.setFocus(Qt.FocusReason.MouseFocusReason)
                control.setFocus(Qt.FocusReason.TabFocusReason)
                self.app.processEvents()
                self.assertTrue(control.property('keyboardFocus'))
                if control in (date, destination):
                    image = control.grab().toImage()
                    self.assertEqual(image.pixelColor(1, image.height() // 2), QColor(theme_colors(theme)['accent']))
                QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
                self.app.processEvents()
                self.assertFalse(bool(control.property('keyboardFocus')))
                date.popup.hide()
            self.first.setFocus(Qt.FocusReason.MouseFocusReason)
            date.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(date, Qt.Key.Key_F4)
            self.app.processEvents()
            self.assertTrue(date.popup.isVisible())
            QTest.keyClick(date.popup, Qt.Key.Key_Escape)
            self.app.processEvents()
            self.assertFalse(date.popup.isVisible())
            self.assertTrue(date.property('keyboardFocus'))
