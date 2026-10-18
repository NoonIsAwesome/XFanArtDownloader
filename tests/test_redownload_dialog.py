"""重新下載的明確選擇與安全預設。Offline copy/replace dialog behavior."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from PyQt6.QtCore import QEvent, QLocale, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog
import i18n
import redownload_dialog as dialogs


class RedownloadDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.old_language, self.old_locale, self.old_palette = i18n.language_mode(), QLocale(), self.app.palette()
        i18n.set_language("zh_TW")
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        i18n.set_language(self.old_language)
        QLocale.setDefault(self.old_locale)
        i18n.install_qt_translator(self.app)
        self.app.setPalette(self.old_palette)

    def dialog(self, count=3):
        dialog = dialogs.RedownloadDialog(count)
        self.dialogs.append(dialog)
        return dialog

    def test_copy_is_default_and_does_not_ask_overwrite_confirmation(self):
        dialog = self.dialog()
        dialog.show()
        self.assertTrue(dialog.copy_radio.isChecked())
        self.assertFalse(dialog.overwrite_radio.isChecked())
        self.assertIn("3", dialog.count_label.text())
        self.assertEqual(dialog.count_label.textFormat(), Qt.TextFormat.PlainText)
        with patch.object(dialogs, "_confirm_overwrite") as confirm:
            QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
            confirm.assert_not_called()
        self.assertEqual(dialog.selected_mode, "copy")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_overwrite_requires_second_confirmation_and_cancel_keeps_choices(self):
        dialog = self.dialog(7)
        dialog.show()
        dialog.overwrite_radio.setChecked(True)
        self.assertTrue(dialog.path_hint.isVisible())
        with patch.object(dialogs, "_confirm_overwrite", return_value=False) as confirm:
            QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
            confirm.assert_called_once_with(7, dialog, "light")
        self.assertIsNone(dialog.selected_mode)
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.overwrite_radio.isChecked())
        with patch.object(dialogs, "_confirm_overwrite", return_value=True):
            QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.selected_mode, "overwrite")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_second_confirmation_enter_and_escape_default_to_cancel(self):
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
            with self.subTest(key=key):
                dialog = dialogs._OverwriteConfirmation(2)
                self.dialogs.append(dialog)
                dialog.show()
                QTest.qWait(5)
                self.assertTrue(dialog.cancel_button.isDefault())
                self.assertFalse(dialog.overwrite_button.isDefault())
                QTest.keyClick(dialog, key)
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                self.assertFalse(dialog.isVisible())

    def test_second_confirmation_requires_explicit_replace_activation(self):
        dialog = dialogs._OverwriteConfirmation(2, theme="dark")
        self.dialogs.append(dialog)
        dialog.show()
        QTest.mouseClick(dialog.overwrite_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_translations_and_themes_preserve_mode_and_fixed_footer(self):
        dialog = self.dialog(40)
        dialog.overwrite_radio.setChecked(True)
        dialog.resize(460, 390)
        dialog.show()
        for language in ("zh_TW", "zh_CN", "en", "ja", "ko"):
            i18n.set_language(language)
            dialog.refresh_language()
            for theme in ("light", "dark"):
                dialog.set_theme(theme)
                self.app.processEvents()
                self.assertTrue(dialog.overwrite_radio.isChecked())
                self.assertEqual(dialog.copy_radio.text(), i18n.tr("另存副本（建議）"))
                self.assertEqual(dialog.start_button.text(), i18n.tr("開始重新下載"))
                self.assertTrue(dialog.rect().contains(dialog.start_button.geometry()))
                self.assertTrue(dialog.rect().contains(dialog.cancel_button.geometry()))
                self.assertFalse(dialog.start_button.geometry().intersects(dialog.cancel_button.geometry()))
                self.assertLessEqual(dialog.start_button.geometry().bottom(), dialog.height() - 1)

    def test_cancel_never_returns_a_mode(self):
        dialog = self.dialog()
        with patch.object(dialogs, "RedownloadDialog", return_value=dialog), patch.object(dialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(dialogs.choose_redownload_mode(3))

    def test_invalid_media_counts_are_rejected(self):
        for count in (0, -1, True, "3"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                dialogs.RedownloadDialog(count)


if __name__ == "__main__":
    unittest.main()
