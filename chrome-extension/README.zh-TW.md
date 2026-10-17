# FanArtDownloader 3.0.1 — Chrome 擴充功能

在 Chrome 中依 X Hashtag 收集原圖與影片，沿用目前 Chrome 的 X 登入。介面、資料庫、抓取與下載佇列都在擴充功能內執行，不需要 Python、EXE、ChromeDriver 或 Cookie 匯出。

## 三步載入

1. 在 Chrome 網址列輸入 `chrome://extensions`。
2. 開啟右上角「開發人員模式」。
3. 按「載入未封裝項目」，選擇**內含 `manifest.json` 的擴充功能資料夾**（倉庫中的 `chrome-extension`）。

載入後可在拼圖選單中釘選 FanArtDownloader，按圖示開啟完整操作分頁。請保留這個資料夾；Chrome 仍會從此位置載入程式。[Chrome 官方載入說明](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world#load-unpacked)

## 開始收集

1. 按「在 X 登入」，在正常 Chrome 的 X 網站完成登入。使用 Google 帳號時，也在正常的 Google／X 網頁操作；帳密只輸入網站，不輸入擴充功能。
2. 輸入一個或多個完整 Hashtag，例如 `#sampleart #另一個標籤`。可用空白、逗號 `,`、中文逗號 `，`、分號 `;` 或中文分號 `；` 分隔。
3. 設定下載子資料夾，按「開始收集」。登入檢查若顯示「尚未確認」，仍可嘗試開始，由 X 實際資料回應再確認登入；明確要求登入時，先回 X 完成登入再繼續。

多標籤採 **OR**：貼文含任一指定完整標籤即可。`#sampleart_extra` 不等於 `#sampleart`。每個標籤分別搜尋，再由本機資料庫去重；搜尋、作者補抓、直接貼文及重試都套用相同完整標籤條件，作者其他未標記作品會略過。引用貼文或轉推的媒體不會冒充原貼文自己的作品。

## 補抓來源與進階設定

- **標籤搜尋**：讀取 X 最新搜尋的分頁資料。
- **作者媒體補抓**：作者欄可填 `@Artist` 或 `https://x.com/Artist/media`，每行一位。可以留空，並保留自動補抓歷史與本次發現作者的選項；完全沒被發現過的作者，需自行補上。作者欄不填數字 ID。
- **直接貼文**：貼上 X／Twitter 貼文網址，仍須符合本次任一指定標籤。
- **進階設定**：可選作者分資料夾、台灣日期起點、全域貼文數上限、下載間隔與失敗重試。上限以本輪去重後的貼文計算，包含已下載而略過的貼文，不是媒體檔案數。

收集以 X 回傳的分頁游標接續，不依賴 DOM 捲動高度或畫面是否已載入圖片。照片保留來源格式並要求原圖；影片優先選擇來源提供的最高位元率 MP4，不會把所有圖片轉成 PNG。

## 下載到哪裡

下載由 Chrome 管理。「下載位置」填的是 Chrome 下載根目錄內的**相對子資料夾**，例如 `FanArt/sampleart`，不能填 `F:\FanArt` 或 `..`。要改根目錄，請到 Chrome「設定 → 下載」調整位置；下載提示與安全檢查仍依 Chrome 設定處理。可從介面開啟 Chrome 下載紀錄查看實際位置。[Chrome downloads API](https://developer.chrome.com/docs/extensions/reference/api/downloads)

完成狀態會核對 Chrome 的下載結果與可取得的檔案資訊；這不等於逐一解碼圖片或證明來源內容完整。

## 停止、續接與備份

工作來源、游標、作者及媒體狀態存於此 Chrome 設定檔的本機 IndexedDB。停止後可繼續；Chrome 暫停背景 worker 時，工作可從已保存的批次接續。**完全關閉 Chrome 時不會繼續工作**；重新開啟 Chrome 後會核對保存的工作與下載狀態，必要時按「繼續收集」。睡眠、斷網或 X 限流可能延後下一批。

「查看紀錄」可匯出及匯入本版的 JSON 紀錄備份。備份包含媒體紀錄與作者，不包含圖片／影片檔案、登入 Cookie、完整工作游標或介面設定。從備份還原的完成紀錄會保留去重，無法證明原電腦的媒體檔案仍存在。

3.0 使用自己的擴充功能資料庫，不會自動讀取舊 EXE 的 SQLite、瀏覽器設定檔或 Cookie 檔案。請保留舊專案與已下載媒體作為備份。

## 更新

先停止工作並匯出紀錄備份，更新**原本相同資料夾**中的程式檔案，再到 `chrome://extensions` 按此項目的「重新載入」，並重新整理操作分頁。保留同一擴充功能 ID 時，本機紀錄與設定會繼續使用。不要為了更新先按「移除」；解除安裝會清除擴充功能儲存資料。移動資料夾、換 Chrome 設定檔或改用商店版本可能產生不同 ID，請先備份。

目前可直接以未封裝方式本機使用，無需先上架。日後若採 Chrome Web Store **Unlisted** 發布，持有連結的人即可安裝，但仍須接受與其他可見性相同的政策及審核；不保證審核成功。本專案尚未因此執行上架操作。[商店發布與可見性](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution)

## 已知限制與驗證範圍

X 官方表示搜尋不一定顯示每篇貼文。作者媒體補抓與直接網址可增加探索來源，仍不能保證全量；刪除、受保護、帳號權限、敏感內容設定、搜尋未收錄、限流，以及沒有指定標籤的作品都會影響結果。[X 搜尋說明](https://help.x.com/en/using-x/x-search-not-working)

本版使用 X 網頁資料介面及目前登入狀態，沒有申請或假造官方付費 API 的 OAuth 授權。X 可能改動介面；若顯示需要更新驗證格式，請更新擴充功能。離線解析、佇列與瀏覽器模擬測試通過，**不等於真實 X 登入與下載已成功**，也不代表所有貼文都能取得。

只有開發測試需要 Node.js 與 `package.json` 中的開發依賴；日常載入擴充功能不需要。開發者可在包含 `tests` 的專案根目錄執行：

```powershell
npm install
npm test
```

資料處理方式見 [PRIVACY.md](PRIVACY.md)。X adapter 與交易驗證碼部分衍生自 gallery-dl 1.32.16，授權及來源見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 與 [LICENSE](LICENSE)。下載媒體的著作權仍屬原作者，程式碼授權不會授予媒體的再利用權利。
