"""No-network avatar fixtures and current-author GUI event boundaries."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QObject, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtNetwork import QNetworkProxy, QNetworkReply, QNetworkRequest
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication
import author_avatar as avatar
import i18n
import main

URL = "https://pbs.twimg.com/profile_images/123/fixture_normal.png"


def png(width=16, height=16):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class FakeReply(QObject):
    metaDataChanged = pyqtSignal()
    readyRead = pyqtSignal()
    downloadProgress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, url):
        super().__init__()
        self.address = QUrl(url)
        self.data = bytearray()
        self.status = 200
        self.length = None
        self.aborted = self.deleted = False
        self.failure = QNetworkReply.NetworkError.NoError

    def setReadBufferSize(self, value):
        self.buffer_limit = value

    def header(self, header):
        return self.length

    def attribute(self, attribute):
        return self.status

    def read(self, count):
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return QByteArray(chunk)

    def bytesAvailable(self):
        return len(self.data)

    def error(self):
        return self.failure

    def url(self):
        return self.address

    def abort(self):
        self.aborted = True
        self.finished.emit()  # Exercise a synchronous abort/finish race too.

    def deleteLater(self):
        self.deleted = True

    def complete(self, data):
        self.data.extend(data)
        self.readyRead.emit()
        self.finished.emit()


class FakeManager(QObject):
    authenticationRequired = pyqtSignal(object, object)
    proxyAuthenticationRequired = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests, self.replies = [], []
        self.proxy = None

    def setCookieJar(self, jar):
        self.jar = jar

    def clearConnectionCache(self):
        pass

    def clearAccessCache(self):
        pass

    def setProxy(self, proxy):
        self.proxy = proxy

    def get(self, request):
        self.requests.append(request)
        reply = FakeReply(request.url())
        self.replies.append(reply)
        return reply


class ClosedNativeReply(QNetworkReply):
    """Exercise the real Qt closed-QIODevice read return type without networking."""
    def __init__(self):
        super().__init__()
        self.setUrl(QUrl(URL))
        self.setAttribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute, 200)
        self.open(QIODevice.OpenModeFlag.ReadOnly)
        self.close()
        self.aborted = False

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def readData(self, maximum):
        return None


class AvatarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.manager = FakeManager()
        self.loader = avatar.AvatarLoader(manager=self.manager)
        self.images = []
        self.loader.ready.connect(self.images.append)

    def test_real_closed_qnetwork_reply_read_returns_none_without_escaping_qt_slot(self):
        reply = ClosedNativeReply()
        self.manager.get = lambda request: reply
        self.loader.load(URL)
        self.assertIsNone(reply.read(1))
        reply.readyRead.emit()
        reply.finished.emit()
        self.assertEqual(self.images, [None])
        self.assertIsNone(self.loader._reply)

    def test_successful_finished_closed_reply_keeps_already_received_image(self):
        reply = ClosedNativeReply()
        self.manager.get = lambda request: reply
        self.loader.load(URL)
        # readyRead already drained a valid response before Qt closed the device.
        self.loader._data.extend(png())
        reply.finished.emit()
        self.assertIsInstance(self.images[-1], QImage)
        self.assertFalse(reply.aborted)
        self.assertIsNone(self.loader._reply)

    def tearDown(self):
        self.loader.close()
        self.loader.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_only_public_profile_cdn_paths_are_accepted(self):
        for url in (URL, "https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png"):
            self.assertTrue(avatar.valid_avatar_url(url))
        for url in ("http://pbs.twimg.com/profile_images/123/a.png", "https://pbs.twimg.com.evil/profile_images/a.png",
                    "https://user:password@pbs.twimg.com/profile_images/a.png", "https://pbs.twimg.com:443/profile_images/a.png",
                    "https://pbs.twimg.com:/profile_images/a.png", URL + "#fragment", "https://pbs.twimg.com/media/a.png",
                    "https://pbs.twimg.com/profile_images/../media/a.png", "https://pbs.twimg.com/profile_images/%2e%2e/a.png",
                    "https://abs.twimg.com/profile_images/a.png", "file:///private.png", None):
            with self.subTest(url=url):
                self.assertFalse(avatar.valid_avatar_url(url))

    def test_no_startup_request_and_credentials_or_redirects_are_never_allowed(self):
        self.assertEqual(self.manager.requests, [])
        self.loader.load(URL)
        request = self.manager.requests[-1]
        self.assertEqual(request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute),
                         QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        for attribute in (QNetworkRequest.Attribute.CookieLoadControlAttribute,
                          QNetworkRequest.Attribute.CookieSaveControlAttribute,
                          QNetworkRequest.Attribute.AuthenticationReuseAttribute):
            self.assertEqual(request.attribute(attribute), QNetworkRequest.LoadControl.Manual)
        self.assertNotIn(b"Cookie", [bytes(name) for name in request.rawHeaderList()])
        self.assertNotIn(b"Authorization", [bytes(name) for name in request.rawHeaderList()])
        self.assertEqual(self.manager.jar.cookiesForUrl(QUrl(URL)), [])
        self.assertFalse(self.manager.jar.setCookiesFromUrl([], QUrl(URL)))
        reply = self.manager.replies[-1]
        reply.status = 302
        reply.metaDataChanged.emit()
        self.assertTrue(reply.aborted)
        self.assertEqual(len(self.manager.requests), 1)
        self.assertIsNone(self.images[-1])

    def test_warp_is_fixed_socks5_with_remote_dns_and_no_direct_fallback(self):
        self.assertEqual(self.manager.proxy.type(), QNetworkProxy.ProxyType.DefaultProxy)
        self.loader.configure(True, 40123)
        proxy = self.manager.proxy
        self.assertEqual((proxy.type(), proxy.hostName(), proxy.port()), (QNetworkProxy.ProxyType.Socks5Proxy, "127.0.0.1", 40123))
        self.assertTrue(proxy.capabilities() & QNetworkProxy.Capability.HostNameLookupCapability)
        self.assertEqual((proxy.user(), proxy.password()), ("", ""))
        self.loader.load(URL)
        self.manager.replies[-1].failure = QNetworkReply.NetworkError.ProxyConnectionRefusedError
        self.manager.replies[-1].finished.emit()
        self.assertIsNone(self.images[-1])
        self.assertEqual(len(self.manager.requests), 1)
        self.assertEqual(self.manager.proxy.type(), QNetworkProxy.ProxyType.Socks5Proxy)

    def test_bytes_pixels_timeout_and_malformed_images_fall_back(self):
        self.assertIsNone(avatar.decode_avatar(png(1001, 1000)))
        self.assertIsNone(avatar.decode_avatar(b"x" * (avatar.MAX_BYTES + 1)))
        self.assertIsNone(avatar.decode_avatar(b"not an image"))
        self.loader.load(URL)
        reply = self.manager.replies[-1]
        reply.complete(b"x" * (avatar.MAX_BYTES + 1))
        self.assertTrue(reply.aborted)
        self.loader.load(URL)
        second = self.manager.replies[-1]
        timed_out = QSignalSpy(self.loader.ready)
        self.loader._timer.start(1)
        self.assertTrue(timed_out.wait(1000))
        self.assertTrue(second.aborted)
        self.assertIsNone(self.images[-1])

    def test_extreme_aspect_ratio_still_has_a_small_square_thumbnail(self):
        for width, height in ((1, 2000), (2000, 1)):
            with self.subTest(width=width, height=height):
                image = avatar.decode_avatar(png(width, height))
                self.assertIsNotNone(image)
                self.assertEqual((image.width(), image.height()), (80, 80))
                self.assertLessEqual(image.sizeInBytes(), 80 * 80 * 4)

    def test_memory_cache_is_small_and_does_not_reload_same_active_url(self):
        with patch.object(avatar, "CACHE_ITEMS", 2):
            for number in range(3):
                url = URL.replace("123", str(number))
                self.loader.load(url)
                self.loader.load(url)
                self.manager.replies[-1].complete(png())
            self.assertEqual(len(self.manager.requests), 3)
            self.assertEqual(len(self.loader._cache), 2)
            self.assertIsInstance(self.images[-1], QImage)
            self.loader.load(URL.replace("123", "2"))
            self.assertEqual(len(self.manager.requests), 3)
            self.loader.load(URL.replace("123", "0"))
            self.assertEqual(len(self.manager.requests), 4)

    def test_stale_stop_close_and_route_change_cannot_replace_current_avatar(self):
        self.loader.load(URL)
        old = self.manager.replies[-1]
        self.loader.load(URL.replace("123", "456"))
        self.assertTrue(old.aborted)
        old.complete(png())
        self.assertEqual(self.images, [])
        self.loader.configure(True, 40000)
        self.assertTrue(self.manager.replies[-1].aborted)
        self.loader.load(URL)
        current = self.manager.replies[-1]
        self.loader.close()
        current.complete(png())
        self.loader.load(URL)
        self.assertEqual(self.images, [])

    def test_gui_keeps_author_text_literal_and_drops_previous_run_events(self):
        previous_language = i18n.language_mode()
        previous_palette = self.app.palette()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.object(avatar, "QNetworkAccessManager", FakeManager):
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "settings.json").write_text(json.dumps({"language":"zh_TW","auto_driver_update":False}), encoding="utf-8")
            window = main.TwitterDownloader(show_notice=False, base_dir=root)
            try:
                self.assertTrue(window.current_media_row.isHidden())
                worker = object()
                window._download_worker = worker
                window.is_downloading = True
                event = {"type":"media","state":"downloading","author_handle":"artist","author_name":"<b>Painter</b>",
                         "avatar_url":URL,"filename":"123_1.png","display_path":"artist\\123_1.png","media_type":"image"}
                window._media_progress(worker, event)
                self.assertEqual(window.current_author_label.literal_text, "<b>Painter</b> · @artist")
                self.assertEqual(window.current_author_label.textFormat(), Qt.TextFormat.PlainText)
                self.assertEqual(window.current_file_label.literal_text, "artist\\123_1.png")
                self.assertEqual(len(window._avatar_loader.manager.requests), 1)
                window._download_worker = object()  # Simulate the next collection run.
                window._media_progress(worker, {**event, "author_handle":"stale"})
                self.assertEqual(window._current_media["author_handle"], "artist")
                window.stop_download()
                window._media_progress(window._download_worker, {**event, "author_handle":"stopped"})
                self.assertEqual(window._current_media["author_handle"], "artist")
                self.assertTrue(window._avatar_loader.manager.replies[-1].aborted)
            finally:
                window._download_worker = None
                window.is_downloading = False
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                i18n.set_language(previous_language)
                i18n.install_qt_translator(self.app)
                self.app.setPalette(previous_palette)

    def test_normal_completion_allows_last_avatar_to_finish_but_stop_aborts_it(self):
        previous_language = i18n.language_mode()
        previous_palette = self.app.palette()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.object(avatar, "QNetworkAccessManager", FakeManager):
            root = Path(directory)
            window = main.TwitterDownloader(show_notice=False, base_dir=root)
            try:
                worker = object()
                window._download_worker = worker
                window.is_downloading = True
                event = {"type":"media","state":"downloading","author_handle":"artist","avatar_url":URL}
                window._media_progress(worker, event)
                reply = window._avatar_loader.manager.replies[-1]
                values = dict(downloaded=1, skipped=0, failed=0, posts=1, filtered=0, errors=[])
                window._download_completed(SimpleNamespace(**values, stopped=False))
                self.assertFalse(reply.aborted)
                reply.complete(png())
                self.assertIsInstance(window._avatar_image, QImage)
                window._media_progress(worker, {**event, "avatar_url":URL.replace("123", "456")})
                reply = window._avatar_loader.manager.replies[-1]
                window._download_completed(SimpleNamespace(**values, stopped=True))
                self.assertTrue(reply.aborted)
            finally:
                window._download_worker = None
                window.is_downloading = False
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                i18n.set_language(previous_language)
                i18n.install_qt_translator(self.app)
                self.app.setPalette(previous_palette)


if __name__ == "__main__":
    unittest.main()
