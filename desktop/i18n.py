"""Application language selection and translation, independent of UI layout.

Catalog keys are the original Traditional Chinese copy. User input, URLs,
credentials and downloaded metadata are never rewritten by widget translation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from PyQt6.QtCore import QLibraryInfo, QLocale, QObject, QTranslator
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QTextEdit, QWidget,
)

LANGUAGES = ('system', 'zh_TW', 'zh_CN', 'en', 'ja', 'ko')
# 繁體中文原文就是翻譯鍵；其他語系只替換介面文案，不翻譯使用者資料。
# Traditional Chinese copy is the translation key; user data is never translated.
_mode = 'system'
_catalogs: dict[str, dict[str, str]] = {}
_templates: dict[str, list] = {}
_PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
_HTML_TAG = re.compile(r'(<[^>]+>)')


def resolve_language(mode: str, system_locale: str | None = None) -> str:
    """Resolve regional variants; unsupported system languages use English."""
    # 明確選擇優先；跟隨系統時區分繁簡中文，不支援的語言退回英文。
    # Honor explicit choices; distinguish Chinese scripts and use English for unsupported locales.
    if mode != 'system':
        return mode if mode in LANGUAGES else resolve_language('system', system_locale)
    system = QLocale.system()
    # Prefer the display-language list over regional number/date formatting.
    # Windows may use English UI with a Taiwanese regional format, or vice versa.
    preferred = system.uiLanguages()
    name = (system_locale or (preferred[0] if preferred else system.name())).replace('-', '_').lower()
    if name.startswith('zh'):
        parts = name.split('_')
        if 'hant' in parts:
            return 'zh_TW'
        if 'hans' in parts:
            return 'zh_CN'
        return 'zh_CN' if any(part in parts for part in ('cn', 'sg')) else 'zh_TW'
    for supported in ('en', 'ja', 'ko'):
        if name == supported or name.startswith(supported + '_'):
            return supported
    return 'en'


def language_mode() -> str:
    return _mode


def effective_language() -> str:
    return resolve_language(_mode)


def locale() -> QLocale:
    return QLocale(effective_language())


def set_language(mode: str) -> None:
    # 保存的是使用者選擇（可為 system），實際 Qt locale 由當下系統語言解析。
    # Preserve the chosen mode, including system, while resolving the effective Qt locale separately.
    global _mode
    _mode = mode if mode in LANGUAGES else 'system'
    QLocale.setDefault(locale())


def _catalog(language: str) -> dict[str, str]:
    # 原始碼與 EXE 共用 locales；檔案缺漏或格式錯誤時保留原文，避免阻止啟動。
    # Use the same catalogs in source/frozen runs; missing or invalid catalogs fall back to source copy.
    if language not in _catalogs:
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'locales'
        path = root / f'{language}.json'
        try:
            content = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            _catalogs[language] = {key: value for key, value in content.items()
                                   if isinstance(key, str) and isinstance(value, str)}
        except (OSError, ValueError, AttributeError):
            # An incomplete development checkout remains usable in source copy.
            _catalogs[language] = {}
    return _catalogs[language]


def _template_pairs(language: str):
    # 後端訊息含動態參數時，以已知模板比對；先嘗試較具體的完整句子。
    # Match dynamic backend messages against known templates, most specific first.
    if language not in _templates:
        pairs = []
        for source, target in _catalog(language).items():
            tokens = list(_PLACEHOLDER.finditer(source))
            if not tokens or len(_PLACEHOLDER.sub('', source).strip()) < 2:
                continue
            fragments, position = [], 0
            for index, match in enumerate(tokens):
                fragments.extend((re.escape(source[position:match.start()]), f'(?P<v{index}>.*?)'))
                position = match.end()
            fragments.append(re.escape(source[position:]))
            specificity = len(_PLACEHOLDER.sub('', source))
            pairs.append((specificity, re.compile(''.join(fragments), re.DOTALL), tokens, target))
        # Prefer complete messages over broad wrappers such as "Source: {name}".
        _templates[language] = [(pattern, tokens, target) for _, pattern, tokens, target
                                in sorted(pairs, key=lambda item: item[0], reverse=True)]
    return _templates[language]


def _translate_plain(text: str, language: str, depth: int = 0) -> str:
    # 依序找完整文案、去除外側空白的文案與模板；找不到就原樣回傳。
    # Try exact copy, trimmed copy, then templates; preserve unknown text unchanged.
    catalog = _catalog(language)
    if text in catalog:
        return catalog[text]
    stripped = text.strip()
    if stripped and stripped in catalog:
        start = len(text) - len(text.lstrip())
        return text[:start] + catalog[stripped] + text[start + len(stripped):]
    # Background workers use source-language messages. Match only known catalog
    # templates so paths, account names and other inserted values stay intact.
    for pattern, tokens, target in _template_pairs(language):
        match = pattern.fullmatch(text)
        if match:
            values = {token.group(1): match.group(f'v{index}') for index, token in enumerate(tokens)}
            if depth < 3:
                # Translate nested, known error/source labels without treating
                # arbitrary paths, account names or post text as UI copy.
                for name in ('exc', 'error', 'message', 'source_name'):
                    if name in values:
                        values[name] = _translate_plain(values[name], language, depth + 1)
            return _PLACEHOLDER.sub(lambda item: values.get(item.group(1), item.group(0)), target)
    return text


def translate_text(text: str) -> str:
    language = effective_language()
    if not isinstance(text, str) or language == 'zh_TW':
        return text
    translated = _translate_plain(text, language)
    if translated != text:
        return translated
    if '<' in text and '>' in text:
        # Only visible text nodes are translated; href/style attributes and
        # markup remain byte-for-byte intact.
        return ''.join(part if index % 2 else _translate_plain(part, language)
                       for index, part in enumerate(_HTML_TAG.split(text)))
    return text


def tr(text: str, **values) -> str:
    # 先翻譯模板再填值，網址、帳號與路徑等插入值不會被當作翻譯鍵。
    # Translate the template before inserting values, keeping URLs, handles and paths literal.
    translated = translate_text(text)
    return translated.format(**values) if values else translated


def _source_text(text: str) -> str:
    """Recover source copy when tr() ran before a widget was first visited."""
    for language in LANGUAGES[2:]:
        for source, target in _catalog(language).items():
            if text == target:
                return source
    if '<' in text and '>' in text:
        parts = _HTML_TAG.split(text)
        if len(parts) > 1:
            return ''.join(part if index % 2 else _source_text(part)
                           for index, part in enumerate(parts))
    return text


def translate_widget_tree(root: QWidget) -> None:
    """Retranslate existing controls without recreating them or touching input."""
    # 只更新標籤、提示及 placeholder；i18nSkip 保護動態資料與自行重翻譯的區塊。
    # Update UI copy only; i18nSkip protects dynamic data and self-translating subtrees.
    for widget in [root, *root.findChildren(QObject)]:
        ancestor = widget
        while ancestor is not None and not ancestor.property('i18nSkip'):
            ancestor = ancestor.parent()
        if ancestor is not None:
            continue
        if isinstance(widget, QWidget):
            widget.setLocale(locale())
        properties = ['toolTip', 'accessibleName', 'accessibleDescription'] if isinstance(widget, QWidget) else []
        if isinstance(widget, QWidget) and widget.isWindow():
            properties.append('windowTitle')
        if isinstance(widget, (QLabel, QAbstractButton, QAction)):
            properties.append('text')
        if isinstance(widget, QGroupBox):
            properties.append('title')
        if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
            properties.append('placeholderText')
        # 記住原始文案與上次結果，切換多次仍從原文翻譯，不連續翻譯譯文。
        # Retain source copy and the last rendering so repeated switches never translate translations.
        originals = getattr(widget, '_i18n_sources', {})
        for name in properties:
            current = widget.property(name)
            if not isinstance(current, str) or not current:
                continue
            previous = originals.get(name)
            source = previous[0] if previous and current == previous[1] else _source_text(current)
            rendered = translate_text(source)
            widget.setProperty(name, rendered)
            originals[name] = (source, rendered)
        widget._i18n_sources = originals
        if isinstance(widget, QComboBox) and not widget.isEditable():
            old_items = getattr(widget, '_i18n_items', {})
            for index in range(widget.count()):
                current = widget.itemText(index)
                previous = old_items.get(index)
                source = previous[0] if previous and current == previous[1] else _source_text(current)
                rendered = translate_text(source)
                widget.setItemText(index, rendered)
                old_items[index] = (source, rendered)
            widget._i18n_items = old_items


def install_qt_translator(app) -> None:
    """Translate Qt's standard buttons, menus and nonnative file dialogs too."""
    # App 自訂文案與 Qt 內建控制項分開處理；替換舊 translator，避免語系疊加。
    # Translate Qt controls separately and replace the old translator instead of stacking locales.
    previous = getattr(app, '_fanart_qt_translator', None)
    if previous is not None:
        app.removeTranslator(previous)
    translator = QTranslator(app)
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    loaded = translator.load(f'qtbase_{effective_language()}', directory)
    if loaded:
        app.installTranslator(translator)
    app._fanart_qt_translator = translator
