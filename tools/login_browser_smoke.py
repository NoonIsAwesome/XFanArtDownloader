"""Opt-in Windows/Chrome integration test using only generated test cookies.

Never point this script at a real profile. Every run creates a new directory.
It does not sign into Google/X and does not prove real-account authentication.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import http.cookiejar
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import login_browser as login


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError('Timed out waiting for test browser shutdown/startup')


def close_own_test_window(process):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    windows = []

    @callback_type
    def visit(window, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        class_name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(window, class_name, len(class_name))
        if pid.value == process.pid and user.IsWindowVisible(window) and class_name.value == 'Chrome_WidgetWin_1':
            windows.append(window)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    def windows_ready():
        windows.clear()
        user.EnumWindows(visit, 0)
        return bool(windows) or process.poll() is not None
    wait_for(windows_ready)
    for window in windows:
        user.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE, own test browser only.
    wait_for(lambda: process.poll() is not None)


def run():
    root = Path(__file__).resolve().parents[1]
    output = root / 'qa'
    output.mkdir(exist_ok=True)
    report = {'checks': [], 'authenticated_google_x': False}
    browser_path = login._find_browser()
    with tempfile.TemporaryDirectory(prefix='generated-browser-', dir=output) as temporary:
        profile = Path(temporary) / 'profile'
        browser = login.LoginBrowser(profile)
        commands = []
        original_launch = browser._launch

        def launch(command):
            commands.append(list(command))
            if '--headless=new' not in command:
                command = command[:-1] + ['--window-position=-32000,-32000', command[-1]]
            return original_launch(command)

        normal_process = None
        seed_process = None
        try:
            with patch.object(browser, '_launch', side_effect=launch), patch.object(login, 'LOGIN_URL', 'about:blank'):
                browser.open()
            normal_process = browser._login_process
            wait_for(lambda: login._profile_locked(profile))
            assert not any('remote-debugging' in arg or 'headless' in arg or 'automation' in arg for arg in commands[0])
            assert not (profile / 'DevToolsActivePort').exists()
            report['checks'].append('Actual visible Chrome starts in ordinary mode with no debug endpoint')
            try:
                browser.save_cookies(Path(temporary) / 'cookies.txt')
            except login.LoginWindowStillOpen:
                pass
            else:
                raise AssertionError('Open profile must refuse export')
            report['checks'].append('Open real Chrome profile refuses cookie export')
            browser.close()
            assert normal_process.poll() is None
            report['checks'].append('App close does not terminate the human login window')
            close_own_test_window(normal_process)
            wait_for(lambda: not login._profile_locked(profile))

            # Seed only this generated profile. No real credentials are used.
            seed_process = subprocess.Popen([
                str(browser_path), f'--user-data-dir={profile}', '--headless=new',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode', 'about:blank',
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            browser._export_process = seed_process
            endpoint = None
            def endpoint_ready():
                nonlocal endpoint
                endpoint = browser._probe_profile_endpoint()
                return bool(endpoint)
            wait_for(endpoint_ready)
            connection = login._CDPConnection(endpoint, time.monotonic() + 15)
            try:
                connection.call('Storage.setCookies', {'cookies': [
                    {'name': 'auth_token', 'value': 'generated-test-auth', 'domain': '.x.com', 'path': '/', 'secure': True, 'httpOnly': True, 'expires': time.time() + 3600},
                    {'name': 'ct0', 'value': 'generated-test-csrf', 'domain': '.x.com', 'path': '/', 'secure': True, 'expires': time.time() + 3600},
                    {'name': 'google_canary', 'value': 'generated-not-a-login', 'domain': '.google.com', 'path': '/', 'secure': True, 'expires': time.time() + 3600},
                ]})
                connection.call('Browser.close')
            finally:
                connection.close()
            seed_process.wait(timeout=15)
            browser._export_process = None
            wait_for(lambda: not login._profile_locked(profile))

            # A fresh App instance must save a closed, existing profile.
            restarted = login.LoginBrowser(profile)
            destination = Path(temporary) / 'cookies.txt'
            count = restarted.save_cookies(destination)
            assert count == 2 and login.validate_cookies(destination) == 2
            jar = http.cookiejar.MozillaCookieJar(str(destination))
            jar.load(ignore_discard=True, ignore_expires=True)
            assert {item.name for item in jar} == {'auth_token', 'ct0'}
            assert all(item.domain.lstrip('.') == 'x.com' for item in jar)
            assert restarted._export_process is None
            wait_for(lambda: not login._profile_locked(profile))
            report['checks'].extend([
                'Chrome persists generated X cookies across close and headless reopen',
                'Fresh App instance saves an existing profile without another login window',
                'Page-target Network.getCookies exports only X cookies, excludes Google canary',
                'Background exporter exits and releases its profile after saving',
            ])
        finally:
            browser.close()
            if seed_process is not None and seed_process.poll() is None:
                seed_process.terminate()
                seed_process.wait(timeout=5)
            if normal_process is not None and normal_process.poll() is None:
                close_own_test_window(normal_process)
            time.sleep(0.3)
    (output / 'login-browser-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    run()
