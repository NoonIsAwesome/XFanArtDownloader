"""Optional console login helper using the same isolated browser as the GUI."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Console tools share the desktop implementation without becoming a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from login_browser import LoginBrowser, import_cookies


def main() -> int:
    parser = argparse.ArgumentParser(description="儲存 FanArtDownloader 的 X 登入 Cookie。")
    parser.add_argument("--import", dest="source", type=Path, help="匯入 Netscape TXT 或舊版 Selenium JSON")
    parser.add_argument("--save-existing", action="store_true", help="儲存既有專用瀏覽器的登入狀態；請先關閉專用登入視窗")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = root / "data" / "cookies.txt"
    browser = None
    try:
        if args.source:
            count = import_cookies(args.source, destination)
        else:
            browser = LoginBrowser(root / "data" / "browser-profile", on_progress=print)
            if not args.save_existing:
                browser.open()
                input("請在專用瀏覽器完成 X／Google 登入，關閉剛才的整個專用登入視窗後，再回到此視窗按 Enter 儲存：")
            count = browser.save_cookies(destination)
        print(f"已儲存 {count} 個 Cookie：{destination}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("已取消。")
        return 130
    except Exception as error:
        print(f"無法儲存 Cookie：{error}")
        return 1
    finally:
        if browser is not None:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
