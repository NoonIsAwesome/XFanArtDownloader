"""Shared native palettes: application surfaces and owned dialogs agree."""
from __future__ import annotations

from PyQt6.QtGui import QColor, QFont, QPalette
from i18n import effective_language
from focus_visibility import install_button_focus_visibility


def ui_font() -> QFont:
    """Use installed native fonts for the chosen writing system, with fallbacks."""
    # 先選符合介面語言的系統字型，再提供跨語系備援，避免中日韓文字缺字。
    # Prefer a native font for the selected language, with CJK-capable fallbacks.
    preferred = {
        "zh_TW": "Microsoft JhengHei UI", "zh_CN": "Microsoft YaHei UI",
        "ja": "Yu Gothic UI", "ko": "Malgun Gothic", "en": "Segoe UI",
    }.get(effective_language(), "Segoe UI")
    font = QFont()
    font.setFamilies(list(dict.fromkeys((preferred, "Segoe UI", "Microsoft JhengHei UI", "Microsoft YaHei UI", "Yu Gothic UI", "Malgun Gothic"))))
    font.setPointSizeF(10)
    return font


# 顏色以用途命名，讓主視窗、日曆與對話框共用明暗狀態，而非各自猜測系統配色。
# Semantic colors keep the main window, calendar, and dialogs on the same explicit theme.
PALETTES = {
    "light": {
        "window": "#FFFFFF", "surface": "#F3F6F7", "input": "#FFFFFF",
        "ink": "#202A30", "muted": "#586971", "border": "#C6D1D7",
        "divider": "#E0E7EA", "accent": "#176C68", "accent_hover": "#105B58",
        "accent_pressed": "#0B4A47", "accent_ink": "#FFFFFF",
        "accent_soft": "#EDF8F6", "accent_soft_ink": "#155F5C",
        "selection": "#CBE9E5", "disabled_bg": "#EAF0F2",
        "disabled_fg": "#74828A", "hover": "#F0F6F5", "pressed": "#E1EEEC",
        "log": "#F5F8F9", "chip_bg": "#EAF7F5", "chip_fg": "#155F5C",
        "chip_border": "#B8DAD6", "coral": "#F48472",
    },
    "dark": {
        "window": "#121B21", "surface": "#1C2931", "input": "#17242C",
        "ink": "#EDF3F5", "muted": "#B1C0C8", "border": "#4E626D",
        "divider": "#334650", "accent": "#75D8CB", "accent_hover": "#91E4D9",
        "accent_pressed": "#5FC5B8", "accent_ink": "#0C302E",
        "accent_soft": "#203C3D", "accent_soft_ink": "#A4EEE3",
        "selection": "#2E615D", "disabled_bg": "#202D35",
        "disabled_fg": "#8B9AA4", "hover": "#273A43", "pressed": "#304950",
        "log": "#16232B", "chip_bg": "#203C3D", "chip_fg": "#A4EEE3",
        "chip_border": "#467E77", "coral": "#F48472",
    },
}


def theme_colors(theme: str = "light") -> dict[str, str]:
    # 回傳副本，避免單一元件修改顏色時影響所有其他視窗。
    # Return a copy so a widget cannot mutate every other window's shared colors.
    return dict(PALETTES.get(theme, PALETTES["light"]))


def apply_palette(widget, theme: str):
    # 原生 Qt 元件也需要明確的 palette；僅設定樣式表可能混入作業系統的底色。
    # Native Qt widgets need explicit palettes too; stylesheets alone can retain OS colors.
    install_button_focus_visibility()
    c = theme_colors(theme)
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "ink",
        QPalette.ColorRole.Base: "input", QPalette.ColorRole.AlternateBase: "surface",
        QPalette.ColorRole.Text: "ink", QPalette.ColorRole.Button: "surface",
        QPalette.ColorRole.ButtonText: "ink", QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.HighlightedText: "accent_ink", QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.LinkVisited: "accent", QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.ToolTipBase: "surface", QPalette.ColorRole.ToolTipText: "ink",
        QPalette.ColorRole.Light: "border", QPalette.ColorRole.Midlight: "surface",
        QPalette.ColorRole.Mid: "border", QPalette.ColorRole.Dark: "divider",
        QPalette.ColorRole.Shadow: "divider", QPalette.ColorRole.BrightText: "ink",
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role, key in roles.items():
            palette.setColor(group, role, QColor(c[key]))
    # 停用狀態另行覆蓋，讓不可操作的欄位仍保有一致且可辨識的文字。
    # Override disabled roles separately so unavailable fields remain consistently readable.
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["disabled_fg"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(c["disabled_bg"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor(c["disabled_bg"]))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def common_stylesheet(theme: str) -> str:
    # 共用輸入、按鈕、捲軸與焦點規則；鍵盤焦點由控制器標記，滑鼠不留下外框。
    # Share input/button/scrollbar rules; the controller distinguishes keyboard focus from clicks.
    c = theme_colors(theme)
    return f"""
        QLabel, QLineEdit, QTextEdit, QDateEdit, QCheckBox, QPushButton, QToolButton,
        QSpinBox, QComboBox {{ color: {c['ink']}; font-size: 14px; }}
        QLabel {{ background: transparent; }}
        QLabel[role="title"] {{ font-size: 28px; font-weight: 700; }}
        QLabel[role="brand"] {{ font-size: 23px; font-weight: 700; }}
        QLabel[role="section"] {{ font-size: 20px; font-weight: 700; }}
        QLabel[role="field"] {{ font-size: 15px; font-weight: 600; }}
        QLabel[role="muted"] {{ color: {c['muted']}; font-size: 13px; }}
        QLineEdit, QTextEdit, QDateEdit, QSpinBox, QComboBox {{
            background: {c['input']}; border: 1px solid {c['border']}; border-radius: 7px;
            padding: 8px 10px; selection-background-color: {c['selection']}; selection-color: {c['ink']}; }}
        QLineEdit:focus, QTextEdit:focus, QDateEdit:focus, QSpinBox:focus, QComboBox:focus {{
            border: 2px solid {c['accent']}; padding: 7px 9px; }}
        QLineEdit:disabled, QTextEdit:disabled, QDateEdit:disabled, QSpinBox:disabled {{
            color: {c['disabled_fg']}; background: {c['disabled_bg']}; border-color: {c['divider']}; }}
        QComboBox {{ padding-right: 28px; }}
        QComboBox:focus {{ padding-right: 27px; }}
        QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
            width: 24px; border: none; background: transparent; }}
        QComboBox QAbstractItemView {{ background: {c['input']}; color: {c['ink']};
            selection-background-color: {c['accent']}; selection-color: {c['accent_ink']};
            border: 1px solid {c['border']}; outline: none; padding: 4px; }}
        QCheckBox {{ spacing: 9px; background: transparent; font-size: 14px; }}
        QCheckBox[role="source"] {{ font-size: 16px; font-weight: 600; }}
        QCheckBox:disabled {{ color: {c['disabled_fg']}; }}
        QCheckBox:focus {{ outline: 1px solid {c['accent']}; }}
        QPushButton {{ color: {c['ink']}; background: {c['input']}; border: 1px solid {c['border']};
            border-radius: 7px; padding: 8px 14px; min-height: 20px; font-size: 14px; }}
        QPushButton:hover {{ background: {c['hover']}; border-color: {c['accent']}; }}
        QPushButton:pressed {{ background: {c['pressed']}; }}
        QPushButton::menu-indicator {{ image: none; width: 0px; height: 0px; }}
        QPushButton[keyboardFocus="true"]:focus {{ border: 2px solid {c['accent']}; padding: 7px 13px; }}
        QPushButton[variant="primary"] {{ background: {c['accent']}; border-color: {c['accent']};
            color: {c['accent_ink']}; font-weight: 600; }}
        QPushButton[variant="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
        QPushButton[variant="primary"]:pressed {{ background: {c['accent_pressed']}; }}
        QPushButton:disabled, QPushButton[variant="primary"]:disabled {{ color: {c['disabled_fg']};
            background: {c['disabled_bg']}; border-color: {c['disabled_bg']}; }}
        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 7px; margin: 1px; }}
        QScrollBar::handle:vertical {{ background: {c['border']}; min-height: 28px; border-radius: 3px; }}
        QScrollBar::handle:vertical:hover {{ background: {c['accent']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QMenu {{ background: {c['input']}; color: {c['ink']}; border: 1px solid {c['border']};
            border-radius: 7px; padding: 4px; font-size: 14px; }}
        QMenu::item {{ padding: 8px 14px; border: 1px solid transparent; border-radius: 4px; }}
        QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['accent_soft_ink']}; }}
        QMenu::item:disabled {{ color: {c['disabled_fg']}; }}
        QMenu::separator {{ height: 1px; background: {c['divider']}; margin: 6px 8px; }}
        QToolTip {{ background: {c['surface']}; color: {c['ink']}; border: 1px solid {c['border']}; padding: 6px; }}
    """


def gallery_stylesheet(theme: str) -> str:
    # 主頁才使用的面板、標籤與下載紀錄樣式，建立在同一份共用規則上。
    # Main-page panels, tags, and download logs build on the common widget rules.
    c = theme_colors(theme)
    return common_stylesheet(theme) + f"""
        QWidget#galleryWindow {{ background: {c['window']}; color: {c['ink']}; }}
        QLabel#runState {{ font-size: 16px; }}
        QLabel#tagSummary {{ color: {c['chip_fg']}; background: {c['chip_bg']}; border: 1px solid {c['chip_border']};
            border-radius: 13px; padding: 6px 12px; font-size: 14px; font-weight: 600; }}
        QFrame#galleryRule {{ background: {c['divider']}; border: none; }}
        QFrame#searchSurface {{ background: {c['surface']}; border-radius: 12px; }}
        QFrame#authorSurface {{ background: {c['accent_soft']}; border-radius: 12px; }}
        QFrame#strictSurface {{ background: {c['accent_soft']}; border-radius: 8px; }}
        QFrame#destinationSurface {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 7px; }}
        QTextEdit#downloadLog {{ background: {c['log']}; border: 1px solid {c['border']};
            border-radius: 10px; padding: 12px; font-size: 13px; }}
        QPushButton#chooseDestination {{ border: none; border-left: 1px solid {c['border']}; border-radius: 0px; padding: 8px; }}
        QToolButton#advancedToggle {{ color: {c['ink']}; border: none; background: transparent;
            text-align: left; padding: 8px 0px; font-size: 15px; }}
        QToolButton#advancedToggle:hover {{ color: {c['accent']}; }}
        QToolButton#advancedToggle[keyboardFocus="true"]:focus {{ background: {c['accent_soft']}; }}
    """


def apply_dialog_theme(dialog, theme: str = "light"):
    # 每個對話框明確套用字型、palette 與樣式，不依賴開啟當下的系統深淺設定。
    # Set each dialog's font, palette, and styles explicitly instead of inheriting the OS theme.
    c = theme_colors(theme)
    dialog.setObjectName("galleryDialog")
    dialog.setFont(ui_font())
    apply_palette(dialog, theme)
    dialog.setStyleSheet(common_stylesheet(theme) + f"""
        QDialog#galleryDialog, QFrame#galleryDialog {{ background: {c['window']}; color: {c['ink']}; }}
        QFrame[role="panel"] {{ background: {c['surface']}; border-radius: 10px; }}
        QLabel[role="dialogTitle"] {{ color: {c['ink']}; font-size: 24px; font-weight: 700; }}
        QLabel[role="dialogBody"] {{ color: {c['ink']}; font-size: 14px; }}
        QLabel[role="muted"] {{ color: {c['muted']}; font-size: 13px; }}
    """)
