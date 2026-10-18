# 維護者發布 / Maintainer publishing

這是維護者專用流程，一般使用者只需下載 App，**不必提供 R2 金鑰**。發布工具不會嵌入使用者 EXE，也不會取得 X Cookie。

This workflow is for maintainers. App users do **not** need R2 credentials. The publishing tools are separate from the user EXE and never access X cookies.

## 本機授權 / Local authorization

先在 Cloudflare 後台建立獨立 `noon-tools-releases` bucket，以及僅限該 bucket 的 **Object Read & Write** R2 token，例如 `NoonToolsPublisher`。不要使用原本的 `singing-stream-savior-updates` bucket 或 Cloudflare REST token。R2 的 bucket 範圍物件權限供 S3 API 使用，見 [官方文件](https://developers.cloudflare.com/r2/api/tokens/)。

Create a dedicated `noon-tools-releases` bucket and an R2 **Object Read & Write** token scoped only to that bucket. Keep it separate from `singing-stream-savior-updates`; do not reuse a Cloudflare REST token. See the [official R2 authentication documentation](https://developers.cloudflare.com/r2/api/tokens/).

雙擊 `tools/configure_r2_publisher.cmd`，依序輸入官方 S3 endpoint、固定 bucket、選用公開網址、Access Key ID 與 Secret Access Key，最後輸入 `y`。兩個金鑰均隱藏輸入，以 DPAPI 加密保存於 `%LOCALAPPDATA%\NoonTools\Publisher\r2-credentials.clixml`。檔案只允許目前 Windows 使用者與 SYSTEM 存取；DPAPI 限原使用者及電腦。不要把金鑰貼到聊天或 GitHub，也不要分享加密設定檔。

Run `tools/configure_r2_publisher.cmd`. Enter the S3 endpoint, pinned bucket, optional public base, and both hidden keys; confirm with `y`. The keys are DPAPI-encrypted for the original Windows user/computer, with a current-user/SYSTEM-only ACL. Never paste keys into chat/GitHub or share the credential file.

`downloads.noonisawesome.dev` 的 Custom Domain 需自行在 bucket 後台綁定並確認 Active。工具只保存網址，不設定 DNS 或公開網域，也不改既有 `updates.noonisawesome.dev`。

Bind the Custom Domain `downloads.noonisawesome.dev` through the Cloudflare dashboard and confirm it is Active. The helper only stores this URL; it never changes DNS or the existing updates domain.

## 四資產發布 / Four-asset release

將以下四檔放入同一資料夾：`XFanArtDownloader.exe`、`SHA256SUMS.txt`、`LICENSE`、`THIRD_PARTY_NOTICES.txt`。checksum manifest 至少包含 EXE 的 SHA256；若列出其他資產也必須相符。此工具固定發布 `fanart-downloader/3.0.0.0/builds/423cc567f1f20762/`，並鎖定已驗證 EXE SHA256 `423cc567f1f2076261684b58b52b590667bee5a64bea01fc0612f7beb88e7083`。未來版本須先更新工具中的明確版本、檔案及 hash，再重新審查。

Place those four files together. `SHA256SUMS.txt` must identify the EXE; any other listed asset must also match. The tool pins the `fanart-downloader/3.0.0.0/builds/423cc567f1f20762/` prefix and the verified EXE digest above. Future releases require an explicit version/hash update and review.

同一版號在正式公開前重新建置時，使用 `builds/<EXE SHA256 前 16 碼>/` 子目錄，並同步更新網站載點，避免一年 immutable 快取提供舊檔。舊物件保留；本機 EXE 及 GitHub Release 可更新為已驗證的新檔。

For a rebuilt version before public release, use a `builds/<first 16 EXE SHA256 characters>/` subfolder and update the website URL to avoid stale immutable caches. Keep previous objects; replace the local EXE and GitHub Release with the verified build.

務必明確啟動 Windows PowerShell 5.1，先做本機 dry run。此步驟不讀憑證、不連網。

Explicitly launch Windows PowerShell 5.1 for the local dry run. It reads no credentials and makes no network requests.

```powershell
& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\tools\publish_r2_release.ps1 -AssetDirectory .\qa\r2-release-assets -ReportPath .\qa\r2-release-plan.json -PublicDownloadBase https://downloads.noonisawesome.dev
```

確認資產及公開結果報告後，在同一命令增加 `-Publish` 才會讀取本機 DPAPI 設定並執行 S3 請求。應在未安裝自訂 TLS callback 的乾淨 PowerShell 程序中執行；連線不經預設代理，也不跟隨重新導向。

Review the local plan, then add `-Publish` to read the DPAPI configuration and contact R2. Use a clean PowerShell process. Requests bypass default proxies and never follow redirects.

全部四檔會先檢查遠端狀態；既有檔必須同時符合 SHA256 metadata、大小及完整 GET 雜湊才可略過。新檔以已簽署的 `If-None-Match: *` 原子條件上傳，競爭時的 412 也須再次完整驗證。任何衝突都停止，**不覆寫、不刪除、不列出 bucket**。每個上傳後再次 HEAD/GET 驗證；正式版採一年 immutable cache。若中途失敗，可能已有部分資產成功，可安全重跑同一批資產；JSON 報告保留公開狀態，不含金鑰、Authorization 或原始 HTTP 錯誤內容。

All assets are checked before any upload. Existing objects are skipped only after SHA256 metadata, size, and a full GET hash match. New objects use a signed atomic `If-None-Match: *`; a competing 412 must also pass full verification. Conflicts stop the release. The tool never overwrites, deletes, or lists bucket objects. Each upload is verified again and uses immutable release caching. Partial releases can safely be rerun; the JSON report contains public progress only.

R2 條件寫入及簽章依據：[R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/)、[AWS SigV4](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html)。合成測試可用同一 PS5.1 執行 `tests/test_r2_release_publisher.ps1`；不讀真實金鑰或連線 bucket。

The [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/) and [AWS SigV4](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html) define the protocol. Run `tests/test_r2_release_publisher.ps1` with PS5.1 for synthetic signature/protection tests without real credentials or bucket requests.
