"""重新下載選項與覆寫確認。Explicit copy/replace choices for completed media."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from i18n import tr, locale as ui_locale
from resources_rc import get_icon_path
from ui_theme import apply_dialog_theme, theme_colors


def _label(role="dialogBody"):
    # 所有說明以純文字呈現，不讓檔名或翻譯字串被 Qt 當成 HTML。
    # Use plain text so filenames and translated copy cannot become Qt HTML.
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setProperty("role", role)
    return label


def _fit_screen(dialog, parent, width=580, height=500):
    # 長翻譯由內文捲動處理，動作按鈕永遠保留在視窗底部。
    # Scroll long translations while keeping action buttons in a fixed footer.
    screen = parent.screen() if parent is not None else QApplication.primaryScreen()
    available = screen.availableGeometry() if screen is not None else None
    safe_width = max(1, available.width() - 48) if available is not None else 760
    safe_height = max(1, available.height() - 56) if available is not None else 700
    dialog.setMinimumSize(min(420, safe_width), min(300, safe_height))
    dialog.setMaximumSize(min(760, safe_width), safe_height)
    dialog.resize(min(width, safe_width), min(height, safe_height))


class _OverwriteConfirmation(QDialog):
    """Default-Cancel boundary before asking the engine to replace files."""

    def __init__(self, count, parent=None, theme="light"):
        super().__init__(parent)
        self.count = count
        self.setProperty("i18nSkip", True)
        self.setWindowIcon(QIcon(get_icon_path()))
        _fit_screen(self, parent, height=340)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(15)
        self.title_label = _label("dialogTitle")
        outer.addWidget(self.title_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        self.count_label, self.explanation_label = _label(), _label()
        body.addWidget(self.count_label)
        body.addWidget(self.explanation_label)
        body.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button, self.overwrite_button = QPushButton(), QPushButton()
        for button in (self.cancel_button, self.overwrite_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        # 覆寫必須是第二次明確選擇；按 Enter 或 Esc 都先走取消。
        # Replacing requires a second explicit choice; Enter/Escape default to Cancel.
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.cancel_button.clicked.connect(self.reject)
        self.overwrite_button.clicked.connect(self.accept)
        outer.addLayout(footer)
        self.refresh_language()
        self.set_theme(theme)

    def refresh_language(self):
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("確認覆寫原檔"))
        self.title_label.setText(tr("確認覆寫原檔"))
        self.count_label.setText(tr("即將重新下載並覆寫 {count} 個原檔。", count=self.count))
        self.explanation_label.setText(tr("此操作不會保留舊檔副本。只有新檔通過驗證後才會替換；下載失敗會保留原檔。"))
        self.cancel_button.setText(tr("取消"))
        self.overwrite_button.setText(tr("確認覆寫"))

    retranslate_ui = refresh_language

    def set_theme(self, theme):
        apply_dialog_theme(self, theme)


def _confirm_overwrite(count, parent=None, theme="light"):
    dialog = _OverwriteConfirmation(count, parent, theme)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


class RedownloadDialog(QDialog):
    """Choose copy by default; replacing needs a separate default-Cancel confirmation."""

    def __init__(self, count, parent=None, theme="light"):
        super().__init__(parent)
        if type(count) is not int or count < 1:
            raise ValueError("RedownloadDialog requires a positive media count")
        self.count = count
        self.theme_mode = "dark" if theme == "dark" else "light"
        self.selected_mode = None
        self.setProperty("i18nSkip", True)
        self.setWindowIcon(QIcon(get_icon_path()))
        _fit_screen(self, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(15)
        self.title_label = _label("dialogTitle")
        outer.addWidget(self.title_label)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(13)
        self.count_label, self.scope_label = _label(), _label("muted")
        body.addWidget(self.count_label)
        body.addWidget(self.scope_label)

        # 原生互斥選項保留方向鍵與空白鍵操作，切換語言不重設使用者選擇。
        # Native exclusive choices keep arrow/Space keys and survive language changes.
        self.mode_group = QButtonGroup(self)
        self.copy_radio, self.overwrite_radio = QRadioButton(), QRadioButton()
        self.copy_description, self.overwrite_description, self.path_hint = _label("muted"), _label("muted"), _label("muted")
        self.mode_group.addButton(self.copy_radio)
        self.mode_group.addButton(self.overwrite_radio)
        body.addSpacing(3)
        for radio, description in ((self.copy_radio, self.copy_description), (self.overwrite_radio, self.overwrite_description)):
            group = QVBoxLayout()
            group.setSpacing(6)
            group.addWidget(radio)
            indented = QHBoxLayout()
            indented.setContentsMargins(27, 0, 0, 0)
            indented.addWidget(description)
            group.addLayout(indented)
            body.addLayout(group)
        body.addWidget(self.path_hint)
        body.addStretch(1)
        self.copy_radio.setChecked(True)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button, self.start_button = QPushButton(), QPushButton()
        self.start_button.setProperty("variant", "primary")
        for button in (self.cancel_button, self.start_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        self.start_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.start_button.clicked.connect(self._submit)
        self.overwrite_radio.toggled.connect(self.path_hint.setVisible)
        self.path_hint.setVisible(False)
        outer.addLayout(footer)
        self.refresh_language()
        self.set_theme(self.theme_mode)

    def _submit(self):
        # 只有覆寫模式會再確認；取消第二視窗時，保留原本選擇以便改成另存。
        # Only replacement asks again; canceling keeps the choices available for saving a copy.
        mode = "overwrite" if self.overwrite_radio.isChecked() else "copy"
        if mode == "overwrite" and not _confirm_overwrite(self.count, self, self.theme_mode):
            return
        self.selected_mode = mode
        self.accept()

    def refresh_language(self):
        self.setLocale(ui_locale())
        self.setWindowTitle(tr("重新下載"))
        self.title_label.setText(tr("重新下載"))
        self.count_label.setText(tr("已選取 {count} 個已完成媒體。", count=self.count))
        self.scope_label.setText(tr("僅下載這些媒體，不會重新搜尋其他貼文。"))
        self.copy_radio.setText(tr("另存副本（建議）"))
        self.copy_description.setText(tr("保留原檔，在原儲存資料夾建立不重複的新檔名。"))
        self.overwrite_radio.setText(tr("覆寫原檔"))
        self.overwrite_description.setText(tr("新檔下載並驗證成功後才替換原檔；失敗時保留原檔。"))
        self.path_hint.setText(tr("原檔必須仍在原儲存目錄內；若已遺失，請重新掃描來源貼文補抓。"))
        self.cancel_button.setText(tr("取消"))
        self.start_button.setText(tr("開始重新下載"))
        self.copy_radio.setAccessibleDescription(self.copy_description.text())
        self.overwrite_radio.setAccessibleDescription(self.overwrite_description.text() + " " + self.path_hint.text())

    retranslate_ui = refresh_language

    def set_theme(self, theme):
        self.theme_mode = "dark" if theme == "dark" else "light"
        apply_dialog_theme(self, self.theme_mode)
        colors = theme_colors(self.theme_mode)
        self.setStyleSheet(self.styleSheet() + f"""
            QRadioButton {{ color:{colors['ink']}; font-size:14px; spacing:10px; padding:3px 0px; }}
            QRadioButton::indicator {{ width:16px; height:16px; border-radius:9px;
                border:1px solid {colors['border']}; background:{colors['input']}; }}
            QRadioButton::indicator:checked {{ background:{colors['accent']}; border-color:{colors['accent']}; }}
            QRadioButton::indicator:hover {{ border-color:{colors['accent']}; }}
            QRadioButton:focus {{ color:{colors['accent_soft_ink']}; }}
        """)


def choose_redownload_mode(count, parent=None, theme="light"):
    """Return 'copy'/'overwrite' only after the corresponding explicit UI choices."""
    dialog = RedownloadDialog(count, parent, theme)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.selected_mode
        return None
    finally:
        dialog.deleteLater()
