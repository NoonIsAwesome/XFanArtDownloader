"""Only explicit confirmation or verified official links leave these dialogs."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox
import update_dialog as dialogs
import i18n


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.previous = i18n.language_mode()
        i18n.set_language("zh_TW")

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            if isinstance(widget, (dialogs.UpdateResultDialog, QMessageBox)):
                widget.close()
                widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        i18n.set_language(self.previous)

    def test_destructive_confirmation_defaults_to_cancel_and_explains_google_and_retained_data(self):
        captured = []
        def inspect(dialog):
            captured.append(dialog)
            self.assertIs(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            self.assertIn("Google", dialog.informativeText())
            self.assertIn("下載檔案、紀錄與設定會保留", dialog.informativeText())
            self.assertIn("不會撤銷", dialog.informativeText())
            self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
            return QMessageBox.StandardButton.Cancel
        with patch.object(QMessageBox, "exec", inspect):
            self.assertFalse(dialogs.confirm_clear_login(theme="dark"))
        self.assertEqual(len(captured), 1)

    def test_update_links_are_explicit_and_untrusted_links_are_not_rendered_or_opened(self):
        result = {"app": {"installed": "3.0.0.0", "latest": "3.1.0.0", "status": "update_available",
                          "url": dialogs.RELEASES_URL, "download_urls": ["https://outside.example/evil.exe"]},
                  "gallery_dl": {"installed": None, "latest": "1.33.0", "status": "unknown", "url": dialogs.PYPI_URL}}
        with patch.object(dialogs.QDesktopServices, "openUrl") as open_url:
            dialog = dialogs.UpdateResultDialog(result)
            labels = [label.text() for label in dialog.findChildren(QLabel)]
            self.assertFalse(any("outside.example" in text for text in labels))
            open_url.assert_not_called()
            dialog._open_link("https://outside.example/evil.exe")
            open_url.assert_not_called()
            dialog._open_link(dialogs.RELEASES_URL)
            self.assertEqual(open_url.call_args.args[0].toString(), dialogs.RELEASES_URL)

    def test_language_and_theme_refresh_reuse_result_and_keep_versions_literal(self):
        result = {"app": {"installed": "3.0.0.0", "latest": "3.1.0.0", "status": "update_available", "url": dialogs.RELEASES_URL}}
        dialog = dialogs.UpdateResultDialog(result)
        original = dialog._component_labels[0][0]
        original_links = set(dialog._allowed_urls)
        with patch.object(dialogs, "tr", side_effect=lambda source, **values: "EN:" + (source.format(**values) if values else source)):
            dialog.retranslate_ui()
            dialog.set_theme("dark")
        self.assertIs(dialog._component_labels[0][0], original)
        self.assertIn("3.0.0.0", original.text())
        self.assertIn("EN:", original.text())
        self.assertEqual(dialog.theme_mode, "dark")
        self.assertEqual(dialog._allowed_urls, original_links)


if __name__ == "__main__":
    unittest.main()
