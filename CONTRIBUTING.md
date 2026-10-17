# 參與開發

程式問題與功能討論請使用 [Issues](https://github.com/NoonIsAwesome/XFanArtDownloader/issues)。安全問題先閱讀 [SECURITY.md](SECURITY.md)。提交程式前先讓變更能在本機重現並通過相應測試；不需要提供真實帳號或 Cookie。

## Windows 開發環境

使用 64 位元 Python 3.12 或專案已驗證的更新版本，在專案根目錄執行：

```powershell
.\setup.cmd
.\.venv\Scripts\python.exe -m pip check
```

`setup.cmd` 只建立本專案 `.venv`，分別依 `requirements.txt` 與 `requirements-gallery.txt` 安裝控制端和獨立解析器。套件來源、TLS 或雜湊驗證失敗時，查明原因或重新下載，不要略過驗證。日常開發直接執行原始碼，避免 `start.cmd` 優先開啟舊 EXE：

```powershell
.\.venv\Scripts\python.exe desktop/main.py
```

## 測試

先執行與變更相關的測試，再跑專案測試：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:QT_QPA_PLATFORM
.\.venv\Scripts\python.exe desktop/main.py --smoke-test
```

單元測試使用 mock、合成 metadata、暫存 SQLite 與合成 Cookie。新增測試時驗證可觀察的行為，例如多來源去重、所有來源套用相同完整標籤、停止後不遺失紀錄、單篇多媒體完整入庫；不要只重寫實作本身作為預期值。

瀏覽器整合測試應在新建暫存專用設定檔中使用 `about:blank`。只測合成 Cookie 的保存、網域範圍、profile 占用與匯出程序退出；不自動登入 Google／X、不讀取真人帳號或日常瀏覽器資料。驗證紀錄必須區分離線／合成測試與使用者實際帳號的結果。

## 多語系與網路選項

`desktop/i18n.py` 集中處理語言選擇、系統語言解析及 Qt 翻譯；`desktop/locales/` 保存簡體中文、英文、日文及韓文 JSON 字典，繁體中文使用原始文案。新增介面文字時同步補齊各字典，保留相同 key、格式欄位與 HTML 連結／樣式。不要翻譯使用者輸入、檔案路徑、網址、帳號名稱或貼文 metadata。語言切換應保留已填資料、日期、主題、紀錄與進行中的工作；相應測試為 `tests/test_i18n.py` 與 `tests/test_main_i18n.py`。

`desktop/network.py` 提供選用的本程式 WARP 路由，預設關閉。只接受固定本機 `127.0.0.1` 的 SOCKS5 連接埠，使用 `socks5h` 將目的 DNS 解析交給代理；不要改系統 DNS、系統代理、WARP 設定或全域 socket 行為。只在 X 取得資料或下載緩慢時選用，不承諾匿名或加速。人工登入瀏覽器仍走原有作業系統網路；啟用代理後失敗需明確回報，不可悄悄改成直連。

`tests/test_network.py` 使用本機合成 SOCKS5 服務驗證端點限制、代理 DNS、握手與不退回直連，不需要安裝 WARP 或連線到 X。媒體邊界測試需涵蓋逐跳 HTTPS 網域白名單、拒絕外站重新導向、宣告長度與實際串流的 1 GiB 上限。不要將本機 fixture 或離線測試寫成真實 WARP 測速或真人登入成功。

擷取器與媒體下載的重新導向規則不同：`extractor/gallery_bridge.py` 拒絕請求重新導向，避免自訂 CSRF header 跟隨外站；媒體下載才允許白名單 CDN 間的有限逐跳重新導向。保留 `tests/test_gallery_bridge.py` 的合成 CSRF 重新導向回歸測試，不能為了來源恢復可用而直接開啟自動跟隨。worker 自己驗證命令列代理參數，不匯入 GPL v3 的 `desktop/network.py`。

## 程序與授權邊界

- 主 App 的自有程式採 **GPL-3.0-only**，根目錄 [LICENSE](LICENSE) 與 [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt) 提供全文。
- 獨立 gallery-dl worker 的自有程式 `extractor/gallery_bridge.py` 另採 **GPL-2.0-only**；gallery-dl 1.32.16 保留其原始 GPL-2.0-only 授權，全文在 [licenses/GPL-2.0.txt](licenses/GPL-2.0.txt)。此範圍以 worker 來源檔的 SPDX 聲明與 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) 為準。
- GUI 的 Python runtime 不得匯入或包含 gallery-dl 程式碼；worker 不得匯入 PyQt6。兩者只透過一般 URL、Cookie 檔案路徑與通用 JSONL metadata 協作，不共享 Python 物件、內部函式或記憶體。
- 主 EXE 與 worker EXE 分別建置，依賴分析明確排除對方的套件。單檔交付將原封的 `gallery-dl-helper.exe` 作為資料放入主 EXE 的自解壓封裝；執行時從 `_MEIPASS` 以獨立子程序啟動，不把 helper 的 Python 程式碼合併到 GUI 的 PYZ 或同一個 interpreter。變更程序邊界、協定或第三方相依套件前，重新核對授權；同包分發本身不代表所有授權條件自動滿足。
- Chrome 擴充功能保留自己目錄內的 GPL version 2 授權與來源聲明，不因主 App 的 GPL version 3 而被重新授權。

請只提交你有權授權的程式或素材；保留原有第三方聲明。對主 App 的貢獻依 GPL-3.0-only，對標示 GPL-2.0-only worker 的貢獻依該範圍授權。作者頭像、商標、圖示及下載媒體不因程式開源而自動取得相同授權；來源或許可不明時先提出問題。

## 封裝與檢查

桌面程式放在 `desktop/`，獨立解析器放在 `extractor/`，圖片放在 `assets/`；`tools/` 提供選用登入和整合驗證工具。`packaging/` 集中兩個程序的 spec、`version.txt` 和 `resources.qrc`。請在專案根目錄執行命令，避免把執行資料寫進原始碼目錄。

```powershell
.\build.cmd
.\dist\XFanArtDownloader.exe --smoke-test
```

`build.cmd` 依序執行 `packaging/gallery-dl-helper.spec` 與 `packaging/XFanArtDownloader.spec`；`packaging/main.spec` 是後者的相容入口。完整發布需包含主 App、獨立 worker、相應授權／NOTICE、完整對應原始碼與建置步驟。交付給使用者的 `XFanArtDownloader.exe` 內附 worker，使用者不需另放一份 side-by-side helper。確認解壓後兩個程序可協作、停止時 helper 的子程序能結束，且各自 Python 模組封裝沒有另一側不應載入的 runtime。保留封裝中 PATH 清理及 ICU DLL 排除規則，避免誤收其他開發工具的 DLL。

更新 gallery-dl、Qt 或其他依賴時，同步更新版本、第三方聲明與必要測試，重新建置兩個 EXE；只更新 `.venv` 不會更新既有 EXE。公開附件前確認不含 `data`、Cookie、設定檔、SQLite、下載內容、私人截圖或測試 profile。

語言字典需收進 GUI 的 frozen 資料；SOCKS5 支援依賴 PySocks，需保留原始 LICENSE，並檢查實際使用它的 GUI／helper 建置 metadata。發布前依固定版本查核安全公告並記錄查詢日期、來源與範圍；不將「沒有已知命中」描述為完整安全保證，也不把含私人路徑或環境資訊的本機 QA 紀錄公開。

## 變更範圍

新增功能前先閱讀 [架構與維護對照](docs/ARCHITECTURE.md#維護與擴充方式)，確認程式責任、資料流及相應測試。介面、下載規則、登入、資料庫與獨立解析器應透過既有入口協作；不要把新功能全部堆入視窗控制器。拆檔時一併更新來源與 frozen 路徑、匯入、測試及打包清單。

PR 請說明問題、修正後行為、測試與仍未驗證的部分。標籤應依貼文本身 metadata 精確比對，作者補抓及直接網址也遵守相同規則。保留 X 可見性、搜尋缺漏、刪除及限流的誠實限制。

不接受為繞過 X／Google 驗證而隱藏自動化、偽裝登入、停用瀏覽器安全限制或存取未授權內容的功能。隨機延遲只用於調整請求頻率，不應宣稱可避過偵測或保證登入成功。
