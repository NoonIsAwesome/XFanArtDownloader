"""Small, credential-free author avatars fetched asynchronously by Qt Network.

Only a job's public media progress event may start a request. Nothing is written
to disk. Redirects are rejected, and an enabled WARP route never falls back to a
direct connection. Qt's SOCKS5 HostNameLookupCapability resolves names remotely:
https://doc.qt.io/qt-6/qnetworkproxy.html#socks5
"""
from __future__ import annotations

from collections import OrderedDict
from urllib.parse import unquote, urlsplit

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QRectF, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkCookieJar, QNetworkProxy, QNetworkReply, QNetworkRequest

MAX_BYTES = 512 * 1024
MAX_PIXELS = 1_000_000
TIMEOUT_MS = 5000
CACHE_ITEMS = 24


def valid_avatar_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return False
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.port is not None or url.fragment:
            return False
        # Inspect the authority too, rejecting even an explicit empty port.
        if url.netloc not in {"pbs.twimg.com", "abs.twimg.com"}:
            return False
        path = unquote(url.path)
        if "\\" in path or any(piece in {".", ".."} for piece in path.split("/")):
            return False
        return ((url.hostname == "pbs.twimg.com" and path.startswith("/profile_images/") and len(path) > 16)
                or (url.hostname == "abs.twimg.com" and path.startswith("/sticky/default_profile_images/")
                    and len(path) > len("/sticky/default_profile_images/")))
    except (TypeError, ValueError):
        return False


def decode_avatar(data: bytes) -> QImage | None:
    if not data or len(data) > MAX_BYTES:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    if bytes(reader.format()).lower() not in {b"png", b"jpeg", b"jpg", b"webp", b"gif", b"avif"}:
        return None
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_PIXELS:
        return None
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull() or image.width() * image.height() > MAX_PIXELS:
        return None
    # Crop before scaling. Expanding a very narrow raster to fill a square can
    # otherwise create an enormous intermediate image despite the input cap.
    side = min(image.width(), image.height())
    square = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
    return square.scaled(80, 80, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)


def avatar_pixmap(image: QImage | None, colors: dict, size=40) -> QPixmap:
    """Render a real avatar or the existing line-icon vocabulary's placeholder."""
    pixels = size * 2
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, pixels, pixels))
    painter.setClipPath(clip)
    painter.fillRect(pixmap.rect(), QColor(colors["accent_soft"]))
    if image is not None and not image.isNull():
        scaled = image.scaled(pixels, pixels, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
        painter.drawImage((pixels - scaled.width()) // 2, (pixels - scaled.height()) // 2, scaled)
    else:
        painter.scale(pixels / 40, pixels / 40)
        painter.setPen(QPen(QColor(colors["accent"]), 1.7))
        painter.drawEllipse(QRectF(14, 8, 12, 12))
        painter.drawArc(QRectF(8, 20, 24, 23), 0, 180 * 16)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


class _NoCookies(QNetworkCookieJar):
    def cookiesForUrl(self, url):
        return []

    def setCookiesFromUrl(self, cookies, url):
        return False


class AvatarLoader(QObject):
    ready = pyqtSignal(object)  # QImage or None; always delivered in the GUI thread.

    def __init__(self, parent=None, *, manager=None):
        super().__init__(parent)
        self.manager = manager or QNetworkAccessManager(self)
        self.manager.setCookieJar(_NoCookies(self.manager))
        self.manager.authenticationRequired.connect(lambda reply, authenticator: reply.abort())
        self.manager.proxyAuthenticationRequired.connect(lambda proxy, authenticator: self.cancel())
        self._reply = None
        self._data = bytearray()
        self._key = None
        self._route = None
        self._cache = OrderedDict()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timeout)
        self.configure(False, 40000)

    def configure(self, warp_proxy: bool, port: int):
        route = int(port) if warp_proxy and type(port) is int and 1 <= port <= 65535 else 0
        if warp_proxy and not route:
            raise ValueError("Invalid local proxy port")
        if route == self._route:
            return
        self.cancel()
        self._route = route
        self.manager.clearConnectionCache()
        self.manager.clearAccessCache()
        if route:
            proxy = QNetworkProxy(QNetworkProxy.ProxyType.Socks5Proxy, "127.0.0.1", route)
            proxy.setCapabilities(QNetworkProxy.Capability.TunnelingCapability |
                                  QNetworkProxy.Capability.HostNameLookupCapability)
        else:
            proxy = QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy)
        self.manager.setProxy(proxy)

    def load(self, url):
        if self._closed:
            return
        key = (self._route, url) if isinstance(url, str) else None
        if key is not None and self._reply is not None and key == self._key:
            return
        self.cancel()
        if not valid_avatar_url(url):
            self.ready.emit(None)
            return
        self._key = key
        if key in self._cache:
            image = self._cache.pop(key)
            self._cache[key] = image
            self.ready.emit(image)
            return
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        for attr in (QNetworkRequest.Attribute.CookieLoadControlAttribute,
                     QNetworkRequest.Attribute.CookieSaveControlAttribute,
                     QNetworkRequest.Attribute.AuthenticationReuseAttribute):
            request.setAttribute(attr, QNetworkRequest.LoadControl.Manual)
        request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
        request.setRawHeader(b"Accept", b"image/png,image/jpeg,image/webp,image/gif,image/avif")
        request.setRawHeader(b"Accept-Encoding", b"identity")
        request.setTransferTimeout(TIMEOUT_MS)
        reply = self.manager.get(request)
        self._reply = reply
        reply.setReadBufferSize(MAX_BYTES + 1)
        reply.metaDataChanged.connect(lambda: self._headers(reply))
        reply.readyRead.connect(lambda: self._read(reply))
        reply.downloadProgress.connect(lambda received, total: self._progress(reply, received, total))
        reply.finished.connect(lambda: self._finished(reply))
        self._timer.start(TIMEOUT_MS)

    def _headers(self, reply):
        if reply is not self._reply:
            return
        size = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            rejected = ((size is not None and int(size) > MAX_BYTES)
                        or (status is not None and 300 <= int(status) < 400))
        except (TypeError, ValueError):
            rejected = True
        if rejected:
            self._reject(reply)

    def _progress(self, reply, received, total):
        if reply is self._reply and (received > MAX_BYTES or total > MAX_BYTES):
            self._reject(reply)

    def _read(self, reply):
        if reply is not self._reply:
            return
        try:
            available = reply.bytesAvailable()
            if available <= 0:
                # A successfully finished QNetworkReply can already be closed;
                # its bytes were drained by readyRead before finished arrived.
                return
            chunk = reply.read(min(available, MAX_BYTES + 1 - len(self._data)))
            if chunk is None:
                self._reject(reply)
                return
            self._data.extend(bytes(chunk))
        except (RuntimeError, TypeError, ValueError):
            # Real QIODevice.read() returns None on a closed/failed device.
            # No exception may escape a Qt signal handler and abort the App.
            self._reject(reply)
            return
        if len(self._data) > MAX_BYTES:
            self._reject(reply)

    def _finished(self, reply):
        if reply is not self._reply:
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._reject(reply)
            return
        self._headers(reply)
        if reply is not self._reply:
            return
        self._read(reply)
        if reply is not self._reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        ok = status == 200 and valid_avatar_url(reply.url().toString())
        image = decode_avatar(bytes(self._data)) if ok else None
        if image is not None:
            self._cache[self._key] = image
            while len(self._cache) > CACHE_ITEMS:
                self._cache.popitem(last=False)
        self._reply = None
        self._timer.stop()
        self._data.clear()
        reply.deleteLater()
        self.ready.emit(image)

    def _reject(self, reply):
        if reply is self._reply:
            self.cancel()
            self.ready.emit(None)

    def _timeout(self):
        if self._reply is not None:
            self._reject(self._reply)

    def cancel(self):
        reply, self._reply = self._reply, None
        self._timer.stop()
        self._data.clear()
        self._key = None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def close(self):
        self._closed = True
        self.cancel()
        self._cache.clear()
