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
_mode = 'system'
_catalogs: dict[str, dict[str, str]] = {}
_templates: dict[str, list] = {}
_PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
_HTML_TAG = re.compile(r'(<[^>]+>)')


def resolve_language(mode: str, system_locale: str | None = None) -> str:
    """Resolve regional variants; unsupported system languages use English."""
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
    global _mode
    _mode = mode if mode in LANGUAGES else 'system'
    QLocale.setDefault(locale())


def _catalog(language: str) -> dict[str, str]:
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
    previous = getattr(app, '_fanart_qt_translator', None)
    if previous is not None:
        app.removeTranslator(previous)
    translator = QTranslator(app)
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    loaded = translator.load(f'qtbase_{effective_language()}', directory)
    if loaded:
        app.installTranslator(translator)
    app._fanart_qt_translator = translator
