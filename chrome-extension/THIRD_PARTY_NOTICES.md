# 第三方來源與授權

## gallery-dl 1.32.16

本版 `x-client.js` 的 X 網頁請求預設值、功能旗標與 endpoint 整合，以及 `x-transaction.js` 的交易驗證碼演算法，衍生自本機安裝的 **gallery-dl 1.32.16**。這些檔案已保留來源與 GPL-2.0-only 聲明，JavaScript 移植與本機介面整合於 2026 年完成。

- 原作者：Mike Fährmann 與 gallery-dl contributors。
- 交易驗證碼原始檔版權：Copyright 2025-2026 Mike Fährmann。
- 授權：GNU General Public License **version 2**；完整原文隨附於 [LICENSE](LICENSE)。
- 官方開發倉庫：[mikf/gallery-dl on Codeberg](https://codeberg.org/mikf/gallery-dl)。
- 官方 GitHub 倉庫與搬移公告：[mikf/gallery-dl](https://github.com/mikf/gallery-dl)。
- 對應來源：`gallery_dl/extractor/twitter.py`、`gallery_dl/extractor/utils/twitter_transaction_id.py`。

此擴充功能以 GPL version 2 散布；保留相應授權、版權聲明及完整對應原始碼。`LICENSE` 自 gallery-dl 1.32.16 已安裝套件的 `licenses/LICENSE` 原樣複製。本版沒有把 Python、gallery-dl 執行環境、Selenium、PyQt 或 ChromeDriver 包進擴充功能。

## XClientTransaction 的上游來源

gallery-dl 的交易驗證碼 helper 註明其適配來源為 [iSarabjitDhiman/XClientTransaction](https://github.com/iSarabjitDhiman/XClientTransaction)。本版透過 gallery-dl 的 GPL version 2 實作進行移植；本項保留其上游歸屬，不表示直接包入另一份執行套件。

## Unicode 大小寫摺疊資料

`case-fold.js` 的 Unicode 15.0.0 完整 case-fold 差異表由 Python `unicodedata` 相關行為產生，用於完整標籤比對。

- 字元資料參考：[Unicode 15.0.0 CaseFolding.txt](https://www.unicode.org/Public/15.0.0/ucd/CaseFolding.txt)。
- 資料原始聲明：© 2022 Unicode®, Inc.
- 官方使用條款與資料授權：[Unicode Terms of Use](https://www.unicode.org/terms_of_use.html)。

Unicode 與 Unicode Logo 是 Unicode, Inc. 的註冊商標。

## 平台、名稱與媒體

Chrome、Google、X／Twitter 及相關服務名稱屬各自權利人。本專案不是 Google 或 X 官方產品，也未聲稱取得其背書。

下載的圖片、影片、作者頭像與其他創作，其著作權仍屬各自權利人。程式碼的 GPL 授權不會變更媒體授權或授予媒體的商業使用、轉載或再散布權利。
