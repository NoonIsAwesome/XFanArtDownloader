# 安全性與問題回報

## 回報方式

一般可公開的問題，請使用 [XFanArtDownloader Issues](https://github.com/NoonIsAwesome/XFanArtDownloader/issues)，附上版本、Windows／瀏覽器版本、已去識別的重現步驟及錯誤訊息。

**不要在 Issue、截圖、附件、日誌或 PR 中放入 Cookie、登入權杖、瀏覽器設定檔、密碼、私人貼文內容或個人資料。** 可使用新建設定檔、合成 Cookie 及虛構帳號名稱重現。

目前尚未確認專用的私下安全回報管道，也未確認 GitHub Private Vulnerability Reporting 已啟用。若問題細節本身敏感，先以不含漏洞細節或憑證的 Issue 請求維護者提供私下聯絡方式；不要將資訊寄往未經確認的作者信箱。這份文件不承諾特定回應時間。

## 登入與保存的範圍

桌面 App 的登入分成兩個階段：

1. 開啟一般可見的 Chrome／Edge，使用本程式自己的 `--user-data-dir`。人工登入階段不啟用 CDP、遠端除錯、Selenium、Headless 或瀏覽器自動操作。使用者在 X／Google 的網站自行輸入帳密及完成驗證。
2. 使用者正常關閉該專用瀏覽器後，App 才以同一瀏覽器及專用設定檔短暫啟動 Headless 匯出程序，開啟 `about:blank`，以僅限本機 loopback 的 CDP 連線取得適用於 X／Twitter 網址的 Cookie，保存後關閉匯出程序。

程式不接管日常 Chrome／Edge 設定檔，不複製或解密既有 Cookie 資料庫，也不應使用全瀏覽器 Cookie 列舉再事後篩選 Google 等其他網域。專用設定檔仍被占用時，應提示使用者正常關閉並重試；不強制結束人工登入視窗或其他 Chrome 程序。

**桌面版會保存登入 Cookie。** `data/cookies.txt` 是供獨立 gallery-dl worker 使用的 Netscape Cookie 檔案；`data/browser-profile/` 也包含專用瀏覽器的登入資料。Cookie 值不能寫入日誌、命令列、錯誤訊息或公開備份。檔案權限會盡可能限制，但這不代表能防止目前 Windows 使用者、管理員或已取得相應權限的程式存取。

Cookie 的格式與已知到期時間通過檢查，不代表 X 伺服器仍接受該登入。Google／X 也可能提出額外驗證；程式不保證登入永久有效。

## 本機資料與發布

- `data/`、`downloads/`、Cookie 檔案、瀏覽器設定檔、SQLite、個人設定及 QA 暫存資料不應提交 Git。
- `.gitignore` 只防止新增追蹤；不能清除先前已提交的秘密。若曾誤傳登入資料，先撤銷受影響的登入，再與維護者處理儲存庫歷史與發布附件。
- 截圖、錯誤回報及測試輸出應只保留必要資訊；公開前檢查帳號、路徑、網址及私人內容。
- 單檔 Windows 交付內含獨立 `gallery-dl-helper.exe`，啟動時解壓至 PyInstaller 暫存目錄，再由 GUI 以子程序呼叫。GUI 的 Python runtime 不匯入 gallery-dl；worker 不匯入 PyQt6。兩者的獨立程序邊界不是對目前 Windows 使用者的安全沙箱；程序邊界及檔案路徑輸入仍需驗證，不能把來自 X 的資料當作命令或程式碼執行。
- 不停用 TLS／套件雜湊驗證，不以 `--no-sandbox`、無限制遠端除錯位址或寬鬆 origin 設定作為登入修正。

本機 CDP 並不是對本機程式不可見的安全隔離。匯出程序應只在必要期間存活，拒絕非 loopback endpoint、重新導向、已退出的匯出程序及不能確認歸屬的瀏覽器連線。

## 測試範圍

單元測試與瀏覽器 smoke 應使用全新暫存專用設定檔、合成 Cookie 及 `about:blank`。不要讀取真人帳號、現有 Cookie 或日常設定檔，也不要由自動測試進行 Google／X 真人登入。

離線測試、模式切換及 Cookie 持續保存測試，不能證明真實帳號已登入、X 已接受請求或資料能完整收集。Chrome 擴充功能是另一個版本，其資料處理說明以 `chrome-extension/PRIVACY.md` 為準。
