"""Per-App proxy routing, with a local fixture and no external connections."""
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
import requests
import network


def read_exact(connection, size):
    value = b''
    while len(value) < size:
        chunk = connection.recv(size - len(value))
        if not chunk:
            raise OSError('Fixture peer closed early')
        value += chunk
    return value


class LocalSocksFixture:
    """Record a SOCKS request, then reject it without accessing its target."""
    def __init__(self, connect_request=False, accept=True):
        self.connect_request, self.accept = connect_request, accept
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.greeting, self.destination, self.error = None, None, None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            with self.listener.accept()[0] as connection:
                connection.settimeout(3)
                self.greeting = read_exact(connection, 3)
                connection.sendall(b'\x05\x00' if self.accept else b'\x05\xff')
                if self.connect_request and self.accept:
                    version, command, reserved, address_type = read_exact(connection, 4)
                    if address_type != 3:
                        raise AssertionError('Destination DNS must be resolved by SOCKS proxy')
                    name = read_exact(connection, read_exact(connection, 1)[0]).decode('ascii')
                    port = int.from_bytes(read_exact(connection, 2), 'big')
                    self.destination = (name, port)
                    connection.sendall(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
        except Exception as error:
            self.error = error
        finally:
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.thread.join(timeout=6)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError('Local fixture did not close')
        if self.error:
            raise self.error


class NetworkTests(unittest.TestCase):
    def test_proxy_is_opt_in_and_port_is_strictly_validated(self):
        self.assertIsNone(network.warp_proxy_url(False))
        self.assertEqual(network.warp_proxy_url(True), 'socks5h://127.0.0.1:40000')
        for port in (True, 0, 65536, '40000'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                network.warp_proxy_url(True, port)

    def test_endpoint_cannot_carry_remote_hosts_credentials_or_url_payloads(self):
        for url in ('socks5h://outside.example:40000', 'http://127.0.0.1:40000',
                    'socks5h://user:secret@127.0.0.1:40000', 'socks5h://127.0.0.1:40000/path',
                    'socks5h://127.0.0.1:40000?token=value', 'socks5h://127.0.0.1:40000#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                network.validate_proxy_url(url)

    def test_route_changes_only_the_given_session(self):
        environment = dict(os.environ)
        original_socket = socket.socket
        with requests.Session() as selected, requests.Session() as other:
            proxy = network.warp_proxy_url(True)
            network.configure_session(selected, proxy)
            self.assertFalse(selected.trust_env)
            self.assertEqual(selected.proxies, {'http':proxy, 'https':proxy})
            self.assertEqual(other.proxies, {})
            self.assertTrue(other.trust_env)
            self.assertTrue(selected.verify)
        self.assertEqual(dict(os.environ), environment)
        self.assertIs(socket.socket, original_socket)

    def test_normal_media_requests_do_not_read_ambient_netrc_credentials(self):
        with requests.Session() as session:
            network.configure_session(session, None)
            with patch('requests.sessions.get_netrc_auth') as read_auth:
                prepared = session.prepare_request(requests.Request('GET', 'https://pbs.twimg.com/media/fixture'))
            read_auth.assert_not_called()
            self.assertNotIn('Authorization', prepared.headers)
            self.assertTrue(session.trust_env)  # Normal proxy/CA support remains.

    def test_availability_probe_sends_no_account_or_destination(self):
        with LocalSocksFixture() as proxy:
            network.check_proxy_available(network.warp_proxy_url(True, proxy.port))
        self.assertEqual(proxy.greeting, b'\x05\x01\x00')
        self.assertIsNone(proxy.destination)

    def test_unavailable_proxy_is_an_explicit_error(self):
        with LocalSocksFixture(accept=False) as proxy:
            with self.assertRaisesRegex(RuntimeError, '未改用直連'):
                network.check_proxy_available(network.warp_proxy_url(True, proxy.port))

    def test_real_requests_uses_proxy_dns_and_never_falls_back(self):
        with LocalSocksFixture(connect_request=True) as proxy, requests.Session() as session:
            network.configure_session(session, network.warp_proxy_url(True, proxy.port))
            real_resolve = socket.getaddrinfo
            resolved_hosts = []
            def record_resolve(host, *args, **kwargs):
                resolved_hosts.append(host)
                return real_resolve(host, *args, **kwargs)
            with patch('socket.getaddrinfo', side_effect=record_resolve):
                with self.assertRaises(requests.exceptions.ConnectionError):
                    session.get('https://pbs.twimg.com/media/generated-fixture', timeout=3)
        self.assertEqual(proxy.destination, ('pbs.twimg.com', 443))
        self.assertTrue(all(host == '127.0.0.1' for host in resolved_hosts))


if __name__ == '__main__':
    unittest.main()
