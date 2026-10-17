"""Offline coverage for author facts, keyboard links and both dialog themes."""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

import author_dialog
from ui_design import theme_colors
import i18n


class AuthorDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        previous_language = i18n.language_mode()
        i18n.set_language("zh_TW")
        i18n.install_qt_translator(self.app)
        def restore_language():
            i18n.set_language(previous_language)
            i18n.install_qt_translator(self.app)
        self.addCleanup(restore_language)
        self.dialogs = []
        root = Path(__file__).resolve().parents[1]
        self.image_patch = patch.object(author_dialog, "get_author_image_path", return_value=str(root / "assets" / "author.png"))
        self.icon_patch = patch.object(author_dialog, "get_icon_path", return_value=str(root / "assets" / "icon.ico"))
        self.image_patch.start()
        self.icon_patch.start()
        self.addCleanup(self.image_patch.stop)
        self.addCleanup(self.icon_patch.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def make_dialog(self, *args, **kwargs):
        dialog = author_dialog.AuthorInfoDialog(*args, **kwargs)
        self.dialogs.append(dialog)
        dialog.show()
        self.app.setActiveWindow(dialog)
        dialog.activateWindow()
        self.app.processEvents()
        return dialog

    def test_author_links_follow_current_singing_stream_savior_information(self):
        with patch.object(author_dialog.QDesktopServices, "openUrl") as opened:
            dialog = self.make_dialog()
        opened.assert_not_called()
        self.assertEqual(dialog.name_label.text(), "沐橙 Noon")
        self.assertFalse(dialog.avatar_label.pixmap().isNull())
        self.assertIn("NPC", dialog.biography_label.text())
        self.assertEqual(dialog.copyright_label.text(), "Copyright © 2026 Noon Is Awesome. No rights reserved.")
        urls = [url for _, _, url in dialog._links]
        self.assertIn("https://noonisawesome.dev/", urls)
        self.assertIn("https://www.youtube.com/@NoonVtuber", urls)
        self.assertIn("https://www.twitch.tv/NoonVtuber", urls)
        self.assertIn("https://www.threads.com/@noonvtuber", urls)
        self.assertIn("https://p.ecpay.com.tw/3604623", urls)
        self.assertIn("https://streamlabs.com/noon_0606/tip", urls)
        self.assertIn("https://reurl.cc/1k0W9Q", urls)
        self.assertFalse(any(url.startswith("mailto:") for url in urls))

    def test_dialog_inherits_parent_theme_and_explicit_theme_overrides_it(self):
        parent = QWidget()
        parent.theme_mode = "dark"
        self.addCleanup(parent.deleteLater)
        self.assertEqual(self.make_dialog(parent).theme_mode, "dark")
        self.assertEqual(self.make_dialog(parent, theme="light").theme_mode, "light")
        self.assertEqual(self.make_dialog().theme_mode, "light")

    def test_live_language_switch_preserves_author_links_theme_and_scroll_without_opening_links(self):
        dialog = self.make_dialog(theme="dark")
        dialog.resize(500, 460)
        self.app.processEvents()
        original_urls = [url for _, _, url in dialog._links]
        with patch.object(author_dialog.QDesktopServices, "openUrl") as opened:
            for language in ("en", "zh_CN", "ja", "ko", "zh_TW", "en"):
                with self.subTest(language=language):
                    i18n.set_language(language)
                    i18n.install_qt_translator(self.app)
                    position = min(30, dialog.scroll_area.verticalScrollBar().maximum())
                    dialog.scroll_area.verticalScrollBar().setValue(position)
                    dialog.retranslate_ui()
                    self.app.processEvents()
                    self.assertEqual(dialog.windowTitle(), i18n.tr("作者資訊"))
                    self.assertEqual(dialog.biography_label.text(), i18n.tr(author_dialog.BIOGRAPHY))
                    self.assertEqual(dialog.close_button.text(), i18n.tr("關閉"))
                    self.assertEqual(dialog.theme_mode, "dark")
                    self.assertEqual(dialog.scroll_area.verticalScrollBar().value(), min(position, dialog.scroll_area.verticalScrollBar().maximum()))
                    self.assertEqual([url for _, _, url in dialog._links], original_urls)
                    for label, source, url in dialog._links:
                        self.assertIn(url, label.text())
                        self.assertEqual(label.accessibleName(), i18n.tr("開啟{label}", label=i18n.tr(source)))
                    if language != "zh_TW":
                        self.assertNotEqual(dialog.biography_label.text(), author_dialog.BIOGRAPHY)
            opened.assert_not_called()

    def test_both_themes_explicitly_color_dialog_text_links_and_close_button(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                dialog = self.make_dialog(theme=theme)
                colors = theme_colors(theme)
                self.assertEqual(dialog.palette().color(QPalette.ColorRole.Window).name().upper(), colors["window"].upper())
                self.assertEqual(dialog.biography_label.palette().color(QPalette.ColorRole.WindowText).name().upper(), colors["ink"].upper())
                self.assertEqual(dialog.close_button.palette().color(QPalette.ColorRole.ButtonText).name().upper(), colors["accent_ink"].upper())
                self.assertTrue(all(colors["accent"] in label.text() for label, _, _ in dialog._links))
                self.assertNotEqual(colors["window"], colors["ink"])
                self.assertGreater(dialog.name_label.font().pixelSize(), dialog.biography_label.font().pixelSize())
                self.assertEqual(dialog.copyright_label.palette().color(QPalette.ColorRole.WindowText).name().upper(), colors["muted"].upper())

    def test_links_have_keyboard_focus_and_return_opens_selected_link(self):
        dialog = self.make_dialog()
        link, _, url = dialog._links[0]
        self.assertTrue(link.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.assertEqual(link.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        link.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertTrue(link.hasFocus())
        with patch.object(author_dialog.QDesktopServices, "openUrl", return_value=True) as opened:
            QTest.keyClick(link, Qt.Key.Key_Return)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toString(), url)
        self.assertTrue(dialog.isVisible())

    def test_tab_visits_links_in_order_and_space_activates_without_closing(self):
        dialog = self.make_dialog(theme="dark")
        first, _, _ = dialog._links[0]
        second, _, url = dialog._links[1]
        first.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(first, Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertTrue(second.hasFocus())
        with patch.object(author_dialog.QDesktopServices, "openUrl", return_value=True) as opened:
            QTest.keyClick(second, Qt.Key.Key_Space)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toString(), url)
        self.assertTrue(dialog.isVisible())

    def test_unrecognised_link_cannot_open_an_arbitrary_url(self):
        dialog = self.make_dialog()
        with patch.object(author_dialog.QDesktopServices, "openUrl") as opened:
            dialog._open_link("file:///private")
            dialog._open_link("https://example.com")
        opened.assert_not_called()

    def test_minimum_size_keeps_close_action_visible_and_scrolls_content(self):
        dialog = self.make_dialog()
        dialog.resize(dialog.minimumSize())
        self.app.processEvents()
        self.assertTrue(dialog.close_button.isVisible())
        button_rect = dialog.close_button.geometry()
        self.assertTrue(dialog.rect().contains(button_rect))
        self.assertEqual(dialog.scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)

    def test_close_button_accepts_dialog_and_escape_rejects_it(self):
        dialog = self.make_dialog()
        QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        other = self.make_dialog()
        QTest.keyClick(other, Qt.Key.Key_Escape)
        self.assertEqual(other.result(), QDialog.DialogCode.Rejected)


if __name__ == "__main__":
    unittest.main()
