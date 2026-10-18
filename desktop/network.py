# SPDX-License-Identifier: GPL-3.0-only
"""Per-download WARP routing; never changes DNS, system proxy or VPN settings."""
from __future__ import annotations

import socket
from urllib.parse import urlsplit


class _NoAmbientAuth:
    """A truthy auth handler prevents Requests from consulting personal netrc."""
    # CDN 不需個人帳密；防止 requests 自動讀取電腦上的 .netrc 登入資訊。
    # CDN requests must not inherit personal credentials from .netrc.
    def __call__(self, request):
        return request


def warp_proxy_url(enabled: bool, port: int = 40000) -> str | None:
    # 只產生本工具的路由設定；不安裝或啟用 WARP，也不改系統 DNS 或 VPN。
    # Configure only this app's routing; never install WARP or alter system DNS/VPN settings.
    if not enabled:
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('WARP 本機代理連接埠必須介於 1 到 65535。')
    # socks5h delegates destination DNS to the proxy as well as the connection.
    return f'socks5h://127.0.0.1:{port}'


def validate_proxy_url(value: str) -> str:
    # 只允許本機 SOCKS5 連接埠，不接受遠端代理、帳密或額外網址參數。
    # Allow only local SOCKS5 endpoints, without remote hosts, credentials, or extra URL components.
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'socks5h' or parsed.hostname != '127.0.0.1'
                or parsed.username or parsed.password or not port
                or parsed.netloc != f'127.0.0.1:{port}'
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('僅接受本機 WARP SOCKS5 代理，不接受遠端代理或帳密。') from None
    return value


def configure_session(session, proxy_url: str | None) -> None:
    # 一般模式保留既有代理與 CA 環境；選用 WARP 時固定走指定本機代理，不退回直連。
    # Keep normal proxy/CA settings, but explicit WARP routing never falls back to direct access.
    # CDN downloads need no account credentials. Keep environment proxy/CA
    # support in normal mode without implicitly loading personal .netrc auth.
    session.auth = _NoAmbientAuth()
    if proxy_url is not None:
        proxy_url = validate_proxy_url(proxy_url)
        # Explicit routing must not be replaced by HTTP(S)_PROXY or NO_PROXY.
        # This changes only this engine's Session, never the socket module.
        session.trust_env = False
        session.proxies = {'http': proxy_url, 'https': proxy_url}


def check_proxy_available(proxy_url: str) -> None:
    """Check local SOCKS5 availability without sending any X URL or credential.

    A responding SOCKS proxy is not proof of WARP identity or a speed gain.
    HTTPS certificate validation remains active for all real requests.
    """
    # 僅以無帳密 SOCKS 握手確認可用性，不送 X 網址；有回應也不保證是 WARP 或會加速。
    # Probe SOCKS without credentials or X URLs; a response proves neither WARP identity nor speed gains.
    parsed = urlsplit(validate_proxy_url(proxy_url))
    try:
        with socket.create_connection(('127.0.0.1', parsed.port), timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(b'\x05\x01\x00')  # SOCKS5, no authentication.
            reply = b''
            while len(reply) < 2:
                chunk = connection.recv(2 - len(reply))
                if not chunk:
                    break
                reply += chunk
            if reply != b'\x05\x00':
                raise OSError('SOCKS5 negotiation failed')
    except OSError:
        raise RuntimeError('無法使用 WARP 本機代理；請先啟用 WARP 的 Local proxy 並確認連接埠。此次未改用直連。') from None
