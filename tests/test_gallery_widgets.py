"""Offline behavior and theme coverage for the date popup and original notice."""

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QDate, QLocale, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDateEdit, QDateTimeEdit, QDialog, QLabel, QTableView

from gallery_widgets import GalleryDateEdit, UserNoticeDialog
from ui_design import theme_colors
import i18n


class DateAndNoticeTests(unittest.TestCase):
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
        self.editor = GalleryDateEdit()
        self.editor.setDate(QDate(2026, 10, 16))
        self.editor.resize(168, 40)
        self.editor.show()
        self.app.processEvents()

    def tearDown(self):
        self.editor.popup.hide()
        self.editor.close()
        self.editor.deleteLater()
        self.app.processEvents()

    def test_real_calendar_click_applies_date_and_hides_popup(self):
        self.editor.show_calendar_popup()
        self.app.processEvents()
        self.assertTrue(self.editor.popup.isVisible())
        calendar = self.editor.calendarWidget()
        self.assertIs(calendar, self.editor.popup.calendar)
        self.assertFalse(calendar.isNavigationBarVisible())
        view = calendar.findChild(QTableView)
        self.assertIsNotNone(view)
        target = None
        for row in range(1, view.model().rowCount()):
            for column in range(view.model().columnCount()):
                index = view.model().index(row, column)
                if str(index.data()) == "15":
                    target = index
                    break
        self.assertIsNotNone(target)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         view.visualRect(target).center())
        self.app.processEvents()
        self.assertEqual(self.editor.date(), QDate(2026, 10, 15))
        self.assertFalse(self.editor.popup.isVisible())

    def test_today_selects_and_closes_but_respects_maximum(self):
        self.editor.setDate(QDate.currentDate().addDays(-20))
        self.editor.show_calendar_popup()
        self.editor.popup.today_button.click()
        self.assertEqual(self.editor.date(), QDate.currentDate())
        self.assertFalse(self.editor.popup.isVisible())
        maximum = QDate.currentDate().addDays(-1)
        self.editor.setMaximumDate(maximum)
        self.editor.show_calendar_popup()
        self.assertFalse(self.editor.popup.today_button.isEnabled())
        self.editor.popup.today_button.click()
        self.assertEqual(self.editor.date(), maximum)

    def test_navigation_and_direct_year_entry_obey_range(self):
        self.editor.setDateRange(QDate(2020, 3, 15), QDate(2022, 10, 20))
        self.editor.setDate(QDate(2021, 8, 11))
        self.editor.show_calendar_popup()
        popup, calendar = self.editor.popup, self.editor.calendarWidget()
        popup.previous_button.click()
        self.assertEqual(calendar.monthShown(), 7)
        popup.next_button.click()
        self.assertEqual(calendar.monthShown(), 8)
        popup.year_input.setValue(2020)
        self.assertEqual(calendar.yearShown(), 2020)
        self.assertFalse(popup.month_actions[0].isEnabled())
        popup.month_actions[2].trigger()
        self.assertEqual(calendar.monthShown(), 3)
        self.assertFalse(popup.previous_button.isEnabled())
        popup.previous_button.click()
        self.assertEqual(calendar.monthShown(), 3)
        popup.month_actions[11].trigger()
        popup.year_input.setValue(2022)
        self.assertEqual((calendar.yearShown(), calendar.monthShown()), (2022, 10))
        self.assertFalse(popup.next_button.isEnabled())
        popup.year_input.setValue(10)
        self.assertEqual(popup.year_input.value(), 2020)
        self.assertEqual(calendar.yearShown(), 2020)

    def test_disabled_or_popup_disabled_cannot_open_native_calendar(self):
        self.assertTrue(self.editor.calendarPopup())
        self.assertFalse(QDateEdit.calendarPopup(self.editor))
        self.editor.setEnabled(False)
        self.editor.show_calendar_popup()
        self.assertFalse(self.editor.popup.isVisible())
        self.assertFalse(self.editor.calendar_button.isEnabled())
        self.editor.setEnabled(True)
        self.editor.setCalendarPopup(False)
        self.editor.show_calendar_popup()
        self.assertFalse(self.editor.calendarPopup())
        self.assertFalse(self.editor.popup.isVisible())
        self.editor.setCalendarPopup(True)
        self.editor.show_calendar_popup()
        self.assertTrue(self.editor.popup.isVisible())

    def test_keyboard_date_editing_and_section_arrows_still_work(self):
        self.editor.lineEdit().setText("2024-02-29")
        self.editor.interpretText()
        self.assertEqual(self.editor.date(), QDate(2024, 2, 29))
        self.editor.setCurrentSection(QDateTimeEdit.Section.DaySection)
        QTest.keyClick(self.editor, Qt.Key.Key_Down)
        self.assertEqual(self.editor.date(), QDate(2024, 2, 28))
        QTest.keyClick(self.editor, Qt.Key.Key_F4)
        self.assertTrue(self.editor.popup.isVisible())
        QTest.keyClick(self.editor.popup, Qt.Key.Key_Escape)
        self.assertFalse(self.editor.popup.isVisible())

    def test_theme_preserves_date_enabled_state_and_themes_real_calendar(self):
        original = self.editor.date()
        for theme in ("dark", "light"):
            self.editor.setEnabled(False)
            self.editor.set_theme(theme)
            colors = theme_colors(theme)
            self.assertEqual(self.editor.date(), original)
            self.assertFalse(self.editor.isEnabled())
            calendar = self.editor.calendarWidget()
            self.assertEqual(calendar.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base), QColor(colors["input"]))
            self.assertEqual(calendar.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base), QColor(colors["disabled_bg"]))
            self.assertEqual(calendar.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText), QColor(colors["ink"]))
            self.assertEqual(calendar.headerTextFormat().foreground().color(), QColor(colors["muted"]))
            for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
                self.assertEqual(calendar.weekdayTextFormat(day).foreground().color(), QColor(colors["ink"]))

    def test_language_switch_preserves_selected_date_browsed_month_and_range_while_localizing_calendar(self):
        self.editor.setDateRange(QDate(2020, 3, 15), QDate(2030, 10, 20))
        self.editor.setDate(QDate(2024, 2, 29))
        self.editor.set_theme("dark")
        self.editor.show_calendar_popup()
        calendar = self.editor.calendarWidget()
        calendar.setCurrentPage(2025, 8)
        for language in ("en", "ja", "ko", "zh_CN", "zh_TW"):
            with self.subTest(language=language):
                i18n.set_language(language)
                i18n.install_qt_translator(self.app)
                self.editor.retranslate_ui()
                self.app.processEvents()
                popup = self.editor.popup
                self.assertEqual(self.editor.date(), QDate(2024, 2, 29))
                self.assertEqual(calendar.selectedDate(), QDate(2024, 2, 29))
                self.assertEqual((calendar.yearShown(), calendar.monthShown()), (2025, 8))
                self.assertEqual(self.editor.minimumDate(), QDate(2020, 3, 15))
                self.assertEqual(self.editor.maximumDate(), QDate(2030, 10, 20))
                self.assertEqual(self.editor._theme, "dark")
                self.assertTrue(popup.isVisible())
                self.assertEqual(calendar.locale().name(), i18n.locale().name())
                self.assertEqual(popup.month_actions[7].text(), i18n.locale().standaloneMonthName(8, QLocale.FormatType.LongFormat))
                self.assertEqual(popup.month_button.text(), i18n.locale().standaloneMonthName(8, QLocale.FormatType.ShortFormat))
                self.assertEqual(popup.today_button.text(), i18n.tr("今天"))
                self.assertEqual(popup.close_button.accessibleName(), i18n.tr("關閉日曆，不更改日期"))
                view = calendar.findChild(QTableView)
                weekdays = [str(view.model().index(0, column).data()) for column in range(view.model().columnCount())]
                self.assertIn(i18n.locale().dayName(1, QLocale.FormatType.ShortFormat), weekdays)

    def test_notice_retranslates_in_place_in_each_language_without_changing_theme_or_modal_behavior(self):
        dialog = UserNoticeDialog(theme="dark")
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        source = "本工具僅供個人非商業用途使用。\n下載的所有圖片與影片著作權均屬原作者所有。"
        for language in ("en", "ja", "ko", "zh_CN", "zh_TW"):
            with self.subTest(language=language):
                i18n.set_language(language)
                i18n.install_qt_translator(self.app)
                dialog.retranslate_ui()
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(dialog.isModal())
                self.assertEqual(dialog.windowTitle(), i18n.tr("使用者提醒"))
                self.assertEqual(dialog.introduction_label.text(), i18n.tr(source))
                self.assertEqual(dialog.accept_button.text(), i18n.tr("我知道了"))
                self.assertEqual(dialog._theme, "dark")
                if language != "zh_TW":
                    self.assertNotEqual(dialog.introduction_label.text(), source)

    def test_notice_has_original_substance_and_accepts_in_both_themes(self):
        expected = (
            "本工具僅供個人非商業用途使用。", "下載的所有圖片與影片著作權均屬原作者所有。",
            "任意將圖片用於商業用途", "未經授權轉載、打包公開分享", "濫用導致 X 平台封鎖",
            "若大量使用造成帳號異常或封鎖，使用者需自行承擔責任。",
        )
        for theme in ("light", "dark"):
            dialog = UserNoticeDialog(theme=theme)
            try:
                self.assertFalse(dialog.isVisible())  # constructor does not exec
                text = "\n".join(label.text() for label in dialog.findChildren(QLabel))
                for phrase in expected:
                    self.assertIn(phrase, text)
                self.assertEqual(dialog.palette().color(QPalette.ColorRole.Window), QColor(theme_colors(theme)["window"]))
                self.assertEqual(dialog.title_label.palette().color(QPalette.ColorRole.WindowText), QColor(theme_colors(theme)["ink"]))
                dialog.show()
                self.app.processEvents()
                dialog.accept_button.click()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertFalse(dialog.isVisible())
                dialog.set_theme("dark" if theme == "light" else "light")
            finally:
                dialog.close()
                dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
