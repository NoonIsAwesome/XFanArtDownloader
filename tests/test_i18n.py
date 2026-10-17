"""Language resolution, safe widget translation and catalog contracts."""
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QTextEdit, QVBoxLayout, QWidget
import i18n


class I18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.previous = i18n.language_mode()

    def tearDown(self):
        i18n.set_language(self.previous)
        i18n.install_qt_translator(self.app)

    def test_system_locale_variants_and_unsupported_fallback(self):
        for name, expected in {'zh_TW':'zh_TW', 'zh-Hant-HK':'zh_TW', 'zh_CN':'zh_CN',
                               'zh-Hans-SG':'zh_CN', 'zh-Hant-CN':'zh_TW', 'zh-Hans-TW':'zh_CN',
                               'en_US':'en', 'ja_JP':'ja',
                               'ko_KR':'ko', 'fr_FR':'en'}.items():
            with self.subTest(name=name):
                self.assertEqual(i18n.resolve_language('system', name), expected)
        self.assertEqual(i18n.resolve_language('ja', 'en_US'), 'ja')

    def test_runtime_switch_does_not_translate_user_content_or_language_names(self):
        catalog = {'目標標籤':'Target tags', '作者帳號':'Author account', '貼文網址':'Post URLs'}
        with patch.dict(i18n._catalogs, {'en':catalog}):
            root = QWidget()
            layout = QVBoxLayout(root)
            label = QLabel('目標標籤'); layout.addWidget(label)
            entry = QLineEdit('目標標籤'); entry.setPlaceholderText('作者帳號'); layout.addWidget(entry)
            editor = QTextEdit(); editor.setPlainText('貼文網址'); editor.setPlaceholderText('貼文網址'); layout.addWidget(editor)
            combo = QComboBox(); combo.addItem('繁體中文'); combo.setProperty('i18nSkip', True); layout.addWidget(combo)
            i18n.set_language('en'); i18n.translate_widget_tree(root)
            self.assertEqual(label.text(), 'Target tags')
            self.assertEqual(entry.text(), '目標標籤')
            self.assertEqual(entry.placeholderText(), 'Author account')
            self.assertEqual(editor.toPlainText(), '貼文網址')
            self.assertEqual(editor.placeholderText(), 'Post URLs')
            self.assertEqual(combo.itemText(0), '繁體中文')
            i18n.set_language('zh_TW'); i18n.translate_widget_tree(root)
            self.assertEqual(label.text(), '目標標籤')
            self.assertEqual(editor.placeholderText(), '貼文網址')
            root.deleteLater()

    def test_already_translated_startup_copy_can_switch_back(self):
        with patch.dict(i18n._catalogs, {'en':{'使用說明':'Help'}}):
            i18n.set_language('en')
            label = QLabel(i18n.tr('使用說明'))
            i18n.translate_widget_tree(label)
            i18n.set_language('zh_TW'); i18n.translate_widget_tree(label)
            self.assertEqual(label.text(), '使用說明')

    def test_literal_angle_brackets_cannot_recurse_as_empty_html(self):
        label = QLabel('User value: abc <> def')
        i18n.translate_widget_tree(label)
        self.assertEqual(label.text(), 'User value: abc <> def')

    def test_rich_text_preserves_urls_and_style_attributes(self):
        with patch.dict(i18n._catalogs, {'en':{'官方網站':'Website'}}):
            i18n.set_language('en')
            original = '<a href="https://noonisawesome.dev/" style="color:#176C68">官方網站</a>'
            self.assertEqual(i18n.translate_text(original), original.replace('>官方網站<', '>Website<'))

    def test_background_message_template_preserves_inserted_path(self):
        with patch.dict(i18n._catalogs, {'en':{'已下載：{path}':'Downloaded: {path}'}}), patch.dict(i18n._templates, {}, clear=True):
            i18n.set_language('en')
            self.assertEqual(i18n.translate_text('已下載：C:/私人資料/file.png'), 'Downloaded: C:/私人資料/file.png')

    def test_catalogs_have_same_keys_and_preserve_format_fields(self):
        root = Path(i18n.__file__).parent / 'locales'
        catalogs = {language:json.loads((root / f'{language}.json').read_text(encoding='utf-8'))
                    for language in ('zh_CN', 'en', 'ja', 'ko')}
        expected = set(catalogs['en'])
        self.assertGreater(len(expected), 100)
        for language, catalog in catalogs.items():
            self.assertEqual(set(catalog), expected, language)
            for source, target in catalog.items():
                with self.subTest(language=language, source=source):
                    self.assertEqual(sorted(re.findall(r'\{([^{}]+)\}', source)),
                                     sorted(re.findall(r'\{([^{}]+)\}', target)))

    def test_nested_known_error_message_is_translated_without_touching_path(self):
        catalog = {'操作失敗：{error}':'Operation failed: {error}', '檔案無法開啟':'File cannot be opened'}
        with patch.dict(i18n._catalogs, {'en':catalog}), patch.dict(i18n._templates, {}, clear=True):
            i18n.set_language('en')
            self.assertEqual(i18n.translate_text('操作失敗：檔案無法開啟'), 'Operation failed: File cannot be opened')

    def test_specific_thread_template_wins_over_a_generic_source_wrapper(self):
        catalog = {'開始來源：{source_name}':'Starting source: {source_name}',
                   '開始來源：{source_name}（貼文 {seed_id} 與同作者留言）':'Starting source: {source_name} (post {seed_id} and author replies)',
                   '同作者留言補抓':'Author replies'}
        with patch.dict(i18n._catalogs, {'en':catalog}), patch.dict(i18n._templates, {}, clear=True):
            i18n.set_language('en')
            self.assertEqual(i18n.translate_text('開始來源：同作者留言補抓（貼文 123 與同作者留言）'),
                             'Starting source: Author replies (post 123 and author replies)')


if __name__ == '__main__':
    unittest.main()
