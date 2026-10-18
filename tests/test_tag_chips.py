"""Tag summaries wrap as separate, literal native chips at narrow widths."""
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication
from tag_chips import TagChips
from ui_theme import apply_dialog_theme


class TagChipsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.widget = TagChips()
        apply_dialog_theme(self.widget, "dark")
        self.widget.resize(160, 160)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_individual_chips_wrap_without_overflow_and_reflow_when_widened(self):
        self.widget.set_tags(("#fanart", "#illustration", "#作品"))
        self.widget.show()
        self.app.processEvents()
        self.assertEqual(len(self.widget.chips), 3)
        self.assertEqual(self.widget.text(), "#fanart  #illustration  #作品")
        for chip in self.widget.chips:
            self.assertLessEqual(chip.geometry().right(), self.widget.width())
            self.assertEqual(chip.indent(), 0)
            self.assertLessEqual(chip.fontMetrics().horizontalAdvance(chip.text()), chip.contentsRect().width())
        self.assertGreater(self.widget.chips[1].y(), self.widget.chips[0].y())
        self.widget.resize(700, 160)
        self.app.processEvents()
        self.assertEqual(len({chip.y() for chip in self.widget.chips}), 1)

    def test_long_tag_retains_full_tooltip_and_states_replace_the_chips(self):
        value = "#" + "作品" * 100
        self.widget.set_tags((value,))
        self.widget.show()
        self.app.processEvents()
        self.assertEqual(self.widget.chips[0].toolTip(), value)
        self.assertEqual(self.widget.chips[0].accessibleName(), value)
        self.assertNotEqual(self.widget.chips[0].text(), value)
        self.widget.setText("尚未填寫標籤")
        self.app.processEvents()
        self.assertEqual(len(self.widget.chips), 1)
        self.assertEqual(self.widget.text(), "尚未填寫標籤")
