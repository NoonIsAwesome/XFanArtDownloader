"""Bounded local-image previews decoded by Qt's thread pool, never from URLs.

Only committed files inside the current output directory may be read. A single
decode runs at a time; newer requests replace the one pending request. Workers
hold no window reference, and cancellation makes late results harmless.
"""
from __future__ import annotations

from pathlib import Path
import threading

from PyQt6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImageReader

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 24_000_000
PREVIEW_SIZE = QSize(520, 320)
FORMATS = {b"png", b"jpeg", b"jpg", b"webp", b"gif", b"bmp", b"avif"}


def preview_signature(value, output_root):
    # 解析實際路徑後確認仍在下載目錄內，並記住大小／修改時間供解碼前後核對。
    # Resolve containment and retain size/mtime for checks before and after decoding.
    try:
        root = Path(output_root).resolve(strict=True)
        path = Path(value).resolve(strict=True)
        if not root.is_dir() or not path.is_relative_to(root) or not path.is_file():
            return None
        info = path.stat()
        if not 0 < info.st_size <= MAX_FILE_BYTES:
            return None
        return str(path), str(root), info.st_mtime_ns, info.st_size
    except (OSError, TypeError, ValueError):
        return None


def decode_preview(signature, cancelled):
    # 背景工作只讀已確認的本機檔案；解碼前先驗格式、像素及取消狀態。
    # Decode verified local files only, checking format, pixels and cancellation first.
    if cancelled.is_set() or preview_signature(signature[0], signature[1]) != signature:
        return None
    reader = QImageReader(signature[0])
    if bytes(reader.format()).lower() not in FORMATS:
        return None
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_PIXELS:
        return None
    reader.setAutoTransform(True)
    reader.setScaledSize(size.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
    if cancelled.is_set():
        return None
    # 預先要求縮小輸出，再核對結果與檔案特徵，避免顯示已遭替換的內容。
    # Request scaled output, then recheck the result and file signature against replacement races.
    image = reader.read()
    if (cancelled.is_set() or image.isNull() or image.width() * image.height() > 520 * 520
            or preview_signature(signature[0], signature[1]) != signature):
        return None
    return image


class _Signals(QObject):
    finished = pyqtSignal(int, object, object)


class _Decode(QRunnable):
    # 工作只持有資料、取消旗標與 signal，不持有或直接操作任何視窗。
    # Workers hold data, a cancellation flag and signals, never window references.
    def __init__(self, generation, signature, cancelled):
        super().__init__()
        self.generation, self.signature, self.cancelled = generation, signature, cancelled
        self.signals = _Signals()

    def run(self):
        try:
            image = decode_preview(self.signature, self.cancelled)
        except Exception:
            image = None
        try:
            self.signals.finished.emit(self.generation, self.signature, image)
        except RuntimeError:
            pass  # The application may have already destroyed signal receivers.


class MediaPreviewLoader(QObject):
    ready = pyqtSignal(object)

    def __init__(self, parent=None, *, pool=None):
        super().__init__(parent)
        self._pool = pool or QThreadPool.globalInstance()
        self._generation = 0
        self._active = None
        self._pending = None
        self._closed = False
        self._last_signature = None
        self._last_image = None

    def load(self, path, output_root):
        # 單次只跑一份解碼；較新的請求覆蓋唯一待處理項目，不堆積整批圖片。
        # Run one decode at a time and keep only the newest pending request.
        if self._closed:
            return
        signature = preview_signature(path, output_root)
        if signature is None:
            self.cancel()
            self.ready.emit(None)
            return
        if signature == self._last_signature:
            self.cancel()
            self.ready.emit(self._last_image)
            return
        if self._active is not None and signature == self._active.signature and not self._active.cancelled.is_set():
            return
        # 世代編號使前次解碼即使稍後完成，也無法覆蓋較新的預覽。
        # Generation IDs prevent late results from replacing a newer preview.
        self._generation += 1
        if self._active is not None:
            self._active.cancelled.set()
            self._pending = (self._generation, signature)
        else:
            self._start(self._generation, signature)

    def _start(self, generation, signature):
        task = _Decode(generation, signature, threading.Event())
        self._active = task
        task.signals.finished.connect(self._finished)
        self._pool.start(task)

    @pyqtSlot(int, object, object)
    def _finished(self, generation, signature, image):
        # signal 回到 GUI 執行緒才更新快取；完成舊任務後再啟動最新待處理項目。
        # Update the cache on the GUI thread, then start the latest pending decode.
        self._active = None
        if not self._closed and generation == self._generation:
            self._last_signature, self._last_image = signature, image
            self.ready.emit(image)
        pending, self._pending = self._pending, None
        if not self._closed and pending is not None:
            self._start(*pending)

    def cancel(self):
        # 不強制中斷影像解碼器；使結果失效並通知工作在可檢查處安全結束。
        # Invalidate results and request cooperative cancellation instead of interrupting a decoder.
        self._generation += 1
        self._pending = None
        if self._active is not None:
            self._active.cancelled.set()

    def close(self):
        self._closed = True
        self.cancel()
        self._last_image = None
        self._last_signature = None
