"""Offline checks for the guide's workflow, native keys and readable themes."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QRect, Qt, QTimer
from PyQt6.QtGui import QDesktopServices, QFontDatabase, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel

from help_dialog import CONNECTION_HELP_URL, PREVIEW_HELP, PRIVACY, SEARCH_LIMIT_HELP, STEPS, TOOLS_HELP, UsageGuideDialog
from ui_theme import theme_colors
import i18n


class UsageGuideDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.name == "nt":
            # Qt's offscreen backend does not enumerate Windows system fonts.
            for filename in ("msjh.ttc", "msjhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
                path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        previous_language = i18n.language_mode()
        i18n.set_language("zh_TW")
        i18n.install_qt_translator(self.app)
        def restore_language():
            i18n.set_language(previous_language)
            i18n.install_qt_translator(self.app)
        self.addCleanup(restore_language)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def make_dialog(self, theme="light", *, show=True):
        dialog = UsageGuideDialog(theme=theme)
        self.dialogs.append(dialog)
        if show:
            dialog.show()
            self.app.setActiveWindow(dialog)
            self.app.processEvents()
        return dialog

    def test_guide_explains_shared_or_tag_gate_and_auto_vs_optional_manual_authors(self):
        dialog = self.make_dialog()
        first = dialog.step_bodies[0].text()
        self.assertIn("請自行填入", first)
        self.assertIn("任一", first)
        self.assertIn("OR", first)
        self.assertIn("搜尋、作者媒體頁與貼文網址的原貼文", first)
        authors = dialog.step_titles[2].text() + dialog.step_bodies[2].text()
        self.assertIn("留空", authors)
        self.assertIn("自動", authors)
        self.assertIn("歷史紀錄", authors)
        self.assertIn("手動", authors)
        self.assertIn("@帳號", authors)
        self.assertIn("/media", authors)
        self.assertIn("不用數字 ID", authors)
        self.assertIn("任一完整目標標籤", authors)
        text = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertNotIn("noonle", text.lower())
        self.assertNotIn("noonvtuber", text.lower())
        self.assertEqual(dialog.findChildren(QCheckBox), [])

    def test_login_sequence_requires_closing_only_the_dedicated_window_before_save(self):
        dialog = self.make_dialog()
        sequence = dialog.login_sequence_label.text()
        self.assertLess(sequence.index("完成登入"), sequence.index("關閉該專用視窗"))
        self.assertLess(sequence.index("關閉該專用視窗"), sequence.index("回 App"))
        self.assertIn("儲存登入狀態", sequence)
        self.assertIn("Google", dialog.step_bodies[1].text())
        self.assertIn("一般瀏覽器", dialog.step_bodies[1].text())
        self.assertIn("保留日常瀏覽器", dialog.login_note_label.text())
        self.assertIn("重新開啟", dialog.login_note_label.text())
        self.assertIn("再由 X 確認", dialog.login_note_label.text())
        self.assertIn("專用瀏覽器設定檔與 X Cookie", dialog.privacy_label.text())
        self.assertIn("請勿上傳或分享", dialog.privacy_label.text())
        self.assertIn("向 X 傳送請求", dialog.privacy_label.text())
        self.assertNotIn("絕對安全", dialog.privacy_label.text())
        self.assertNotIn("Google 已驗證", dialog.privacy_label.text())

    def test_directory_stop_retained_history_and_advanced_limits_are_explained(self):
        dialog = self.make_dialog()
        text = dialog.step_bodies[3].text()
        for required in ("儲存目錄", "開始收集", "停止", "紀錄會保留", "台灣時間", "搜尋貼文上限", "繼續作者補抓", "無法保證收齊"):
            self.assertIn(required, text)
        self.assertIn("著作權屬原作者", dialog.copyright_label.text())

    def test_search_quota_stops_only_search_and_explains_why_totals_can_exceed_it(self):
        dialog = self.make_dialog()
        text = dialog.search_limit_label.text()
        self.assertIn("所有標籤搜尋共用", text)
        self.assertIn("不重複", text)
        self.assertIn("包含已下載者", text)
        self.assertIn("達上限只停止標籤搜尋，作者媒體補抓仍繼續", text)
        self.assertIn("貼文網址、失敗重試、作者媒體頁與同作者留言附件不占搜尋配額", text)
        self.assertIn("總處理貼文數與媒體下載數可能超過搜尋上限", text)
        self.assertIn("作者媒體頁仍逐篇核對標籤與日期", text)
        self.assertIn("不代表已收齊", text)

    def test_themes_recolor_native_surfaces_text_and_primary_close_on_the_same_dialog(self):
        dialog = self.make_dialog()
        sequence = dialog.login_sequence_label.text()
        for theme in ("light", "dark", "light"):
            with self.subTest(theme=theme):
                dialog.set_theme(theme)
                self.app.processEvents()
                c = theme_colors(theme)
                for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                    self.assertEqual(dialog.palette().color(group, QPalette.ColorRole.Window).name().upper(), c["window"].upper())
                    self.assertEqual(dialog.step_bodies[0].palette().color(group, QPalette.ColorRole.WindowText).name().upper(), c["ink"].upper())
                    self.assertEqual(dialog.login_sequence_label.palette().color(group, QPalette.ColorRole.WindowText).name().upper(), c["accent_soft_ink"].upper())
                    self.assertEqual(dialog.close_button.palette().color(group, QPalette.ColorRole.ButtonText).name().upper(), c["accent_ink"].upper())
                self.assertEqual(dialog.copyright_label.palette().color(QPalette.ColorRole.WindowText).name().upper(), c["muted"].upper())
                self.assertEqual(dialog.login_sequence_label.text(), sequence)
                self.assertFalse(dialog.guide_icon.pixmap().isNull())
        dialog.apply_theme("invalid")
        self.assertEqual(dialog.theme_mode, "light")

    def test_small_screen_scrolls_complete_wrapped_plain_text_and_keeps_close_visible(self):
        screen = MagicMock()
        screen.availableGeometry.return_value = QRect(0, 0, 1020, 700)
        with patch.object(QApplication, "primaryScreen", return_value=screen):
            dialog = self.make_dialog()
        self.assertLessEqual(dialog.frameGeometry().height(), 700)
        self.assertLessEqual(dialog.width(), 760)
        dialog.resize(600, 440)
        self.app.processEvents()
        self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)
        self.assertEqual(dialog.scroll_area.horizontalScrollBar().maximum(), 0)
        center = dialog.close_button.mapTo(dialog, dialog.close_button.rect().center())
        self.assertTrue(dialog.rect().contains(center))
        for label in [*dialog.step_titles, *dialog.step_bodies, dialog.login_sequence_label,
                      dialog.login_note_label, dialog.thread_label, dialog.search_limit_label,
                      dialog.preview_label, dialog.connection_label, dialog.privacy_label, dialog.copyright_label]:
            self.assertTrue(label.wordWrap())
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        dialog.scroll_area.setFocus()
        before = dialog.scroll_area.verticalScrollBar().value()
        QTest.keyClick(dialog.scroll_area, Qt.Key.Key_PageDown)
        self.app.processEvents()
        self.assertGreater(dialog.scroll_area.verticalScrollBar().value(), before)

    def test_nonmodal_first_run_and_modal_help_both_close_with_native_keyboard(self):
        dialog = self.make_dialog()
        self.assertFalse(dialog.isModal())
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())
        modal = self.make_dialog(show=False)
        QTimer.singleShot(0, lambda: QTest.keyClick(modal, Qt.Key.Key_Escape))
        self.assertEqual(modal.exec(), QDialog.DialogCode.Rejected)
        self.assertFalse(modal.isVisible())
        accepted = self.make_dialog()
        accepted.close_button.setFocus()
        QTest.keyClick(accepted.close_button, Qt.Key.Key_Return)
        self.assertEqual(accepted.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(accepted.isVisible())

    def test_opening_and_closing_guide_never_starts_browser_network_or_external_link(self):
        with patch("subprocess.Popen") as process, patch("urllib.request.urlopen") as network, \
                patch.object(QDesktopServices, "openUrl") as opened:
            dialog = self.make_dialog()
            dialog.close_button.click()
            self.app.processEvents()
        process.assert_not_called()
        network.assert_not_called()
        opened.assert_not_called()

    def test_slow_connection_advice_opens_only_the_official_link_after_an_explicit_action(self):
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            dialog = self.make_dialog()
            for url in ("file:///private", "https://example.com", CONNECTION_HELP_URL + "?redirect=other"):
                dialog._open_connection_help(url)
            opened.assert_not_called()
            dialog.connection_help_button.click()
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toString(), CONNECTION_HELP_URL)
        for phrase in ("不會自動更改系統 DNS", "自行安裝官方 WARP", "Preferences", "Configure Proxy", "Local proxy",
                       "40000", "登入瀏覽器沿用作業系統", "不會自動改為直接連線", "1.1.1.1", "WARP", "流量路徑",
                       "不能保證", "不代表匿名", "紀錄會保留"):
            self.assertIn(phrase, dialog.connection_label.text())
        self.assertTrue(dialog.isVisible())

    def test_initial_and_live_languages_use_original_keys_without_losing_scroll_or_theme(self):
        dialog = self.make_dialog(theme="dark")
        dialog.resize(600, 440)
        self.app.processEvents()
        for language in ("en", "ja", "ko", "zh_CN", "zh_TW", "en"):
            with self.subTest(language=language):
                i18n.set_language(language)
                i18n.install_qt_translator(self.app)
                position = min(60, dialog.scroll_area.verticalScrollBar().maximum())
                dialog.scroll_area.verticalScrollBar().setValue(position)
                dialog.retranslate_ui()
                self.app.processEvents()
                self.assertEqual(dialog.windowTitle(), i18n.tr("FanArt Downloader 使用說明"))
                self.assertEqual(dialog.step_bodies[0].text(), i18n.tr(STEPS[0][1]))
                self.assertEqual(dialog.privacy_label.text(), i18n.tr(PRIVACY))
                self.assertEqual(dialog.search_limit_label.text(), i18n.tr(SEARCH_LIMIT_HELP))
                self.assertEqual(dialog.preview_label.text(), i18n.tr(PREVIEW_HELP))
                self.assertEqual(dialog.close_button.text(), i18n.tr("關閉說明"))
                for source in ("下載設定", "X 登入", "更多功能", "使用說明"):
                    self.assertIn(i18n.tr(source), dialog.introduction_label.text())
                self.assertEqual(dialog.theme_mode, "dark")
                self.assertEqual(dialog.scroll_area.verticalScrollBar().value(), min(position, dialog.scroll_area.verticalScrollBar().maximum()))
                self.assertEqual(dialog.locale().name(), i18n.locale().name())
                if language != "zh_TW":
                    self.assertNotEqual(dialog.step_bodies[0].text(), STEPS[0][1])
                    self.assertNotEqual(dialog.privacy_label.text(), PRIVACY)
                    self.assertNotEqual(dialog.search_limit_label.text(), SEARCH_LIMIT_HELP)
                    self.assertNotEqual(dialog.preview_label.text(), PREVIEW_HELP)
                fresh = self.make_dialog(show=False)
                self.assertEqual(fresh.step_bodies[0].text(), i18n.tr(STEPS[0][1]))
                self.assertEqual(fresh.close_button.text(), i18n.tr("關閉說明"))

    def test_preview_guide_points_to_the_two_main_window_tabs_in_every_language(self):
        self.assertIn("主視窗分為", PREVIEW_HELP)
        self.assertIn("選取一筆完成紀錄即可預覽本機圖片", PREVIEW_HELP)
        self.assertNotIn("紀錄與圖片預覽", PREVIEW_HELP)
        self.assertNotIn("詳細下載狀況", PREVIEW_HELP)
        self.assertIn("不會另開視窗", TOOLS_HELP)
        self.assertNotIn("查看紀錄與預覽", TOOLS_HELP)
        for language in ("en", "ja", "ko", "zh_CN", "zh_TW"):
            with self.subTest(language=language):
                i18n.set_language(language)
                translated = i18n.tr(PREVIEW_HELP)
                self.assertIn(i18n.tr("下載設定"), translated)
                self.assertIn(i18n.tr("下載狀況與預覽"), translated)
                self.assertIn(i18n.tr("下載狀況與預覽"), i18n.tr(TOOLS_HELP))
                self.assertIn(i18n.tr("工作與來源進度"), i18n.tr(TOOLS_HELP))
                self.assertIn("🖼️", translated)
                self.assertIn("🎬", translated)
                self.assertIn("\\", translated)
                if language != "zh_TW":
                    self.assertNotEqual(translated, PREVIEW_HELP)


if __name__ == "__main__":
    unittest.main()
