# X FanArt Downloader

Windows 桌面 FanArt 收藏工具。支援多個完整 Hashtag、作者媒體頁補抓、貼文網址下載與 SQLite 去重紀錄，保留明亮畫廊介面與深／淺色切換。

目前桌面版本：**3.0.0.0**。Chrome 擴充版是可獨立使用的另一版本，原始碼位於 `chrome-extension/`。

桌面版維持 gallery-dl、SQLite 與每位使用者的本機登入，不使用付費 X 官方 API，也不內建共用帳號或 Cookie。

## 這個版本改了什麼

- 「搜尋貼文上限」只限制標籤搜尋；達上限後仍繼續作者媒體補抓，直接網址與失敗重試也不受此搜尋配額限制。
- 收集時顯示目前作者及公開頭像，並從本機已完成的下載顯示最近圖片預覽；圖片與影片的成功紀錄以 🖼️／🎬 及簡短路徑區分。
- 語言選單明確標示用途；滑鼠操作不留下持續高亮的按鈕焦點框，鍵盤 Tab 導覽仍保留焦點提示。視窗及 EXE 沿用歌回救星的共同圖示。
- 人工登入使用普通 Chrome／Edge 專用視窗；登入時沒有遠端除錯、自動控制或內嵌瀏覽器。
- 關閉該專用視窗後，App 才短暫啟動背景程序儲存 X 登入狀態，不需要 Chrome 擴充功能或開發人員模式。
- 登入、儲存及匯入 Cookie 分為獨立操作，失敗可重試，不會突然跳出選檔視窗。
- 關閉 App 直接結束；「關於作者」保留在按鈕中，只有點選才顯示。
- 新安裝的目標標籤為空白，由使用者自行輸入。已保存的個人設定不會因升級被重設。
- 語言可選「跟隨電腦設定、繁體中文、簡體中文、英文、日文、韓文」；預設依電腦顯示語言，第一次開啟的說明也會翻譯，選擇會保存在本機。
- X 載入或下載較慢時，可選用僅作用於本 App 的 WARP 本機代理；預設關閉，連線正常時不需要啟用。
- GUI 與 gallery-dl 解析器分成獨立程序，原始碼、授權及資料交換邊界均可檢查。

## 功能

- 多標籤採 OR：貼文含任一指定的**完整標籤**即可。`#sampleart_extra` 不等於 `#sampleart`。
- 搜尋各標籤、補抓自動發現或手動指定作者的媒體頁，以及貼上已知貼文網址。
- 搜尋、作者媒體頁與貼上網址的**原貼文都套用相同標籤條件**；作者其他未標記的作品會略過。作者可填 `@帳號` 或媒體頁 URL，不需要數字 ID；自動發現可不填手動帳號。
- 自動下載圖片與影片。可補抓已符合標籤之原貼文的**同作者留言附件**，留言本身可沒有標籤；須驗證同一作者與回覆關聯，不收其他作者、引用或無法確認關聯的附件。此選項預設開啟，可在進階設定關閉。
- 透過資料分頁取得貼文，不依靠畫面滾動；下載前以貼文與媒體編號去重。
- 支援日期起點、搜尋貼文上限、作者資料夾、失敗補抓與停止。日期仍適用於所有來源的原貼文；符合資格的同作者留言使用原貼文起點的日期條件。
- 搜尋上限由所有標籤共用，計算不重複且符合標籤與日期的搜尋原貼文，包含已下載者。達上限只停止標籤搜尋，仍繼續作者補抓；直接網址、失敗重試、作者媒體頁及同作者留言附件都不消耗搜尋配額，總處理貼文數與媒體數可能超過上限。作者頁作品仍須符合標籤與日期，同一篇及其討論串的媒體不因搜尋配額而截斷。
- 傳輸及內容驗證後才標記完成；既有紀錄會核對檔案，遺失或不完整者可補抓。

X 搜尋、帳號權限與平台提供的資料仍可能遺漏作品，本工具不保證全量，也不保證 X 未來改版後仍能使用。

搜尋只會發現 X 提供的含媒體結果；如果標籤在無媒體的原貼文，而圖片在作者留言中，可直接貼上原貼文網址補抓。X 隱藏、刪除或無法驗證關聯的留言仍可能略過，程式會對不完整來源回報，不能把沒有回傳當成收齊。

若 X 限制討論串請求頻率，本輪會暫停後續同作者留言補抓，並繼續搜尋與作者媒體頁；未完成留言可稍後重試。來源錯誤會顯示登入、頻率限制、連線或擷取工具等固定原因，方便判斷下一步；不直接顯示可能含認證資料的上游原始例外。

作者頭像透過公開的 X 頭像 CDN 另行讀取，不帶 X Cookie 或帳號認證；失敗時使用替代圖，不影響下載。啟用 WARP 時，頭像也使用該工作的本機代理，失敗不切回直連。頭像僅保留有限的記憶體快取；最近圖片預覽只讀目前下載目錄內已完成的本機圖片，不會為預覽重新下載或上傳檔案。圖片過大或無法安全解碼時可能不顯示預覽。

成功下載紀錄的簡短路徑：啟用作者資料夾時顯示 `作者帳號\檔名`，關閉時只顯示檔名；完整下載目錄仍由使用者選擇。

## 啟動桌面版

執行環境為 Windows 64 位元與已安裝的 Chrome 或 Edge。原始碼使用 Python 3.12；一般使用者執行 EXE 不需另裝 Python。

最後交付為單一 **`XFanArtDownloader.exe`**。執行 `start.cmd`，或直接啟動該 EXE；不需另放解析器、安裝 Python 或 Chrome 擴充功能。獨立解析器與原始授權已包含在自解壓封裝內，執行時暫存展開並以另一個程序運作。

本倉庫提供可自行檢查、建置的原始碼；實際提供的二進位版本以倉庫的發佈內容為準，不要從不明來源取得 Cookie 匯出工具。

### 登入順序

1. 按「開啟瀏覽器登入」，在普通 Chrome／Edge **專用視窗**內自行登入 X。使用 Google 帳號時，照 X／Google 網站流程操作。
2. 確認 X 已登入，**關閉剛才的專用瀏覽器視窗**。
3. 回 App 按「儲存登入狀態」，待儲存完成後開始收集。

日常 Chrome 視窗不用關，App 不讀取其設定檔。專用視窗仍開啟時會提示關閉後重試，不會強制結束它。重開 App 後，保留的專用設定檔已關閉時可直接儲存。「匯入 Cookie 檔」是獨立的選用入口。

Google 仍可能要求帳號安全驗證；修正登入方式不代表所有帳號都已實測成功。詳細步驟及故障排除見 [登入說明](docs/LOGIN.md)。

### X 連線較慢與 WARP

一般連線正常時維持預設即可。程式使用有限的等待與重試，未完成項目保留補抓紀錄；不會把來源逾時當作已完整收集。單一媒體上限為 1 GiB，超過時保留失敗紀錄，避免異常回應持續寫滿磁碟。

若想讓本 App 經過 WARP，先自行安裝官方 Cloudflare WARP，依其版本在偏好設定的進階選項啟用 **Local proxy**，確認連接埠（通常是 `40000`），再於 App 的「進階設定」勾選「透過 WARP 本機代理連線」並填相同連接埠。搜尋、作者補抓及媒體下載會透過本機 SOCKS5 代理，目的地 DNS 也交給代理。代理失敗會回報錯誤，**不會自動改用直連**。

此選項不安裝或啟動 WARP、不切換 WARP 模式，也不修改 Windows DNS、系統代理或其他程式。登入瀏覽器沿用電腦既有網路；停止工作或關閉 App 即結束本 App 的代理連線，WARP 本身由使用者管理。它不保證加速、匿名或突破 X 權限。詳見 [Cloudflare 官方模式說明](https://developers.cloudflare.com/warp-client/warp-modes/)；若 WARP 版本或管理政策不提供 Local proxy，請維持 App 的此選項關閉。

## 原始碼安裝與開發

```powershell
git clone https://github.com/NoonIsAwesome/XFanArtDownloader.git
cd XFanArtDownloader
.\setup.cmd
.\start.cmd
```

`setup.cmd` 只在專案建立 `.venv`，從官方 PyPI 安裝固定版本依賴。`requirements.txt` 為 GUI／控制端，`requirements-gallery.txt` 為獨立解析器。不要略過 TLS 驗證。

原始碼依責任分類；桌面程式與解析器維持獨立程序：

```text
desktop/          Qt 介面、登入、下載控制與 SQLite
desktop/locales/  集中管理的簡中、英、日、韓翻譯
extractor/        獨立 gallery-dl 命令列解析器
assets/           桌面版圖片與圖示
packaging/        PyInstaller specs、版本資訊與資源清單
tools/            選用登入與整合驗證工具
tests/            離線測試
docs/             登入說明與架構文件
chrome-extension/ 獨立 Chrome 擴充版
licenses/         原始授權全文
```

`start.cmd` 優先開啟既有 EXE；測試修改後的原始碼時請直接執行：

```powershell
.\.venv\Scripts\python.exe desktop/main.py
```

測試與建置：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe extractor/gallery_bridge.py --smoke-test
.\.venv\Scripts\python.exe desktop/main.py --smoke-test
.\build.cmd
.\dist\gallery-dl-helper.exe --smoke-test
.\dist\XFanArtDownloader.exe --smoke-test
```

`build.cmd` 依序使用 `packaging/gallery-dl-helper.spec` 與 `packaging/XFanArtDownloader.spec`，先建置獨立解析器，再將其作為資料檔放入 GUI 的單檔封裝。GUI 的 Python 程式仍排除 gallery-dl；解析器排除 Qt，執行時不合併進 GUI 程序。請保留現有 Windows ICU／PATH 隔離設定，以免其他工具的 DLL 導致 `QtCore` 啟動失敗。只更新 `.venv` 不會更新既有 EXE。

單元測試主要離線執行。另有選用的 `tools/login_browser_smoke.py`，以全新暫存 Chrome 設定檔與合成 Cookie 檢查普通視窗、關閉、背景擷取及清理，**不登入真人 Google／X 帳號，也不讀取日常設定檔**。不要把測試設定檔改指向自己的帳號資料。

## 本機資料與安全

| 位置 | 內容 |
| --- | --- |
| `data/cookies.txt` | 提供下載解析器使用的 X 登入 Cookie，等同敏感登入資料 |
| `data/browser-profile/` | App 專用 Chrome／Edge 設定檔，可能包含 Google 等網站登入狀態 |
| `data/downloads.sqlite3` | 下載歷史、貼文資料與重試狀態 |
| `data/settings.json` | 介面與收集設定；實際名稱以程式為準 |
| 使用者選定的下載目錄 | 圖片、影片與個人檔案路徑 |

本工具需要保存 X Cookie 才能讓 gallery-dl 在瀏覽器關閉後工作。開源提供檢查方式，**不代表不保存登入資料，也不代表沒有安全風險**。專用設定檔與 Cookie 只保存在本機；不會上傳至開發者伺服器，也不匯出 Google Cookie。下載查詢仍會傳送給 X。

不要把 `data`、Cookie、設定檔、SQLite、私人下載或完整操作紀錄提交到 GitHub；`.gitignore` 已排除執行資料，但不會自動從既有 Git 歷史移除秘密。安全邊界與回報方式見 [SECURITY.md](SECURITY.md)。

## Chrome 擴充版

`chrome-extension/` 不需桌面 EXE、Python 或 ChromeDriver，沿用所在 Chrome 設定檔的 X 登入。下載位置由 Chrome 管理，紀錄存在本機 IndexedDB，與桌面 SQLite 分開，不會自動匯入舊紀錄。

首次在 `chrome://extensions` 開啟「開發人員模式」，選「載入未封裝項目」並選取該資料夾。後續從工具列圖示開啟。此版本尚未聲稱通過 Chrome 商店審核。

詳見 [擴充版 README](chrome-extension/README.zh-TW.md) 與 [隱私說明](chrome-extension/PRIVACY.md)。

## 授權、著作權與貢獻

- 桌面 GUI／控制端：GPL-3.0-only，見 [LICENSE](LICENSE)。
- 獨立 `extractor/gallery_bridge.py`／gallery-dl helper：GPL-2.0-only；Chrome 擴充版保留目錄內的 GPL v2 授權。
- 各第三方函式庫保留原授權，見 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。圖片、品牌素材與下載媒體不因程式碼開源而自動獲得再利用許可。

請遵守 X 使用規範與原作者授權；「個人非商業用途」不是任意轉載或下載受限制內容的許可。X 條款對自動擷取有額外限制，請自行確認適用範圍。本工具不提供繞過登入、權限或付費限制的保證。

程式架構見 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，貢獻與測試約定見 [CONTRIBUTING.md](CONTRIBUTING.md)。作者資訊保留為可主動開啟的選用介面。

官方網站：[noonisawesome.dev](https://noonisawesome.dev/)。也可以試試超讚的歌回直播軟體；App 內的連結只在點選後開啟網站。
