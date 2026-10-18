#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$AssetDirectory,
    [string]$ReportPath,
    [switch]$Publish,
    [string]$PublicDownloadBase = '',
    [switch]$LibraryOnly
)

$publisherLibraryOnly = $LibraryOnly
. (Join-Path $PSScriptRoot 'configure_r2_publisher.ps1') -LibraryOnly
$LibraryOnly = $publisherLibraryOnly
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:R2ReleasePrefix = 'fanart-downloader/3.0.0.0/'
$script:R2ReleaseExeSha256 = '4381989fb1ccb79d9f66a82e1e60e3561d41dc9a7a2e19aa58a4ee7cbe5fc6d3'
$script:R2ReleaseNames = @('XFanArtDownloader.exe', 'SHA256SUMS.txt', 'LICENSE', 'THIRD_PARTY_NOTICES.txt')

function Get-R2HexHash([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Get-R2StreamHash([IO.Stream]$Stream) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream.Position = 0
        ([BitConverter]::ToString($hash.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    } finally { $Stream.Position = 0; $hash.Dispose() }
}

function Invoke-R2Hmac([byte[]]$Key, [string]$Value) {
    $hmac = [Security.Cryptography.HMACSHA256]::new($Key)
    try { ,$hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)) }
    finally { $hmac.Dispose() }
}

function New-R2SignedHeaders {
    param([string]$Method, [string]$HostName, [string]$CanonicalPath, [string]$Query = '',
          [string]$PayloadHash, [hashtable]$Headers = @{}, [Security.SecureString]$AccessKeyId,
          [Security.SecureString]$SecretAccessKey, [DateTime]$Now = [DateTime]::UtcNow,
          [string]$Region = 'auto')
    # SigV4 僅在記憶體使用解密金鑰；輸出只交給 HTTPS 請求，不印出簽章或授權。
    # Decrypt keys only in memory for SigV4; signed headers go directly to HTTPS, never console or reports.
    $stamp = $Now.ToUniversalTime().ToString('yyyyMMddTHHmmssZ', [Globalization.CultureInfo]::InvariantCulture)
    $day = $stamp.Substring(0, 8)
    $values = @{'host'=$HostName; 'x-amz-date'=$stamp; 'x-amz-content-sha256'=$PayloadHash}
    foreach ($name in $Headers.Keys) {
        $lower = $name.ToLowerInvariant()
        if ($lower -notmatch '^[a-z0-9-]+$' -or [string]$Headers[$name] -match '[\r\n\x00]') { throw 'Invalid signing header.' }
        $values[$lower] = ([string]$Headers[$name]).Trim() -replace '\s+', ' '
    }
    $names = @($values.Keys | Sort-Object -CaseSensitive)
    $canonicalHeaders = ($names | ForEach-Object { $_ + ':' + $values[$_] + "`n" }) -join ''
    $signedNames = $names -join ';'
    $canonical = $Method + "`n" + $CanonicalPath + "`n" + $Query + "`n" + $canonicalHeaders + "`n" + $signedNames + "`n" + $PayloadHash
    $canonicalHash = Get-R2HexHash ([Text.Encoding]::UTF8.GetBytes($canonical))
    $scope = $day + '/' + $Region + '/s3/aws4_request'
    $toSign = 'AWS4-HMAC-SHA256' + "`n" + $stamp + "`n" + $scope + "`n" + $canonicalHash
    $accessPointer = [IntPtr]::Zero
    $secretPointer = [IntPtr]::Zero
    $seed = $dateKey = $regionKey = $serviceKey = $signingKey = $null
    try {
        $accessPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AccessKeyId)
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecretAccessKey)
        $access = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($accessPointer)
        if ($access -notmatch '^[A-Za-z0-9]{16,128}$') { throw 'Invalid access key format.' }
        $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
        $seed = [Text.Encoding]::UTF8.GetBytes('AWS4' + $secret)
        $secret = $null
        $dateKey = Invoke-R2Hmac $seed $day
        $regionKey = Invoke-R2Hmac $dateKey $Region
        $serviceKey = Invoke-R2Hmac $regionKey 's3'
        $signingKey = Invoke-R2Hmac $serviceKey 'aws4_request'
        $signature = ([BitConverter]::ToString((Invoke-R2Hmac $signingKey $toSign))).Replace('-', '').ToLowerInvariant()
        $values['authorization'] = 'AWS4-HMAC-SHA256 Credential=' + $access + '/' + $scope + ',SignedHeaders=' + $signedNames + ',Signature=' + $signature
        [pscustomobject]@{ Headers=$values; CanonicalHash=$canonicalHash; Signature=$signature }
    } finally {
        if ($accessPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($accessPointer) }
        if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
        foreach ($key in @($seed, $dateKey, $regionKey, $serviceKey, $signingKey)) {
            if ($null -ne $key) { [Array]::Clear($key, 0, $key.Length) }
        }
        $access = $secret = $null
    }
}

function New-R2ReleasePlan {
    param([Parameter(Mandatory=$true)][string]$Directory,
          [string]$ExpectedExeSha256 = $script:R2ReleaseExeSha256)
    # 固定四個資產與此正式版本，讀鎖防止計算雜湊後檔案被換掉。
    # Pin four assets and this release; retain read locks so files cannot change after hashing.
    $root = Assert-R2PublisherPlainPath ([IO.Path]::GetFullPath($Directory))
    if (-not [IO.Directory]::Exists($root)) { throw 'Asset directory is missing.' }
    $assets = @()
    try {
        foreach ($name in $script:R2ReleaseNames) {
            $path = Assert-R2PublisherPlainPath (Join-Path $root $name)
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            $asset = [pscustomobject]@{ Name=$name; Key=$script:R2ReleasePrefix+$name; Stream=$stream;
                Size=$stream.Length; Sha256=''; ContentType='text/plain; charset=utf-8' }
            $assets += $asset
            $maximum = if ($name -eq 'XFanArtDownloader.exe') { 512MB } elseif ($name -eq 'SHA256SUMS.txt') { 64KB } else { 4MB }
            if ($stream.Length -lt 1 -or $stream.Length -gt $maximum) { throw 'Asset size is invalid.' }
            $asset.Sha256 = Get-R2StreamHash $stream
            if ($name -eq 'XFanArtDownloader.exe') {
                if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90 -or $asset.Sha256 -cne $ExpectedExeSha256) { throw 'Release EXE identity is incorrect.' }
                $stream.Position = 0
                $asset.ContentType = 'application/octet-stream'
            }
        }
        $sums = @($assets | Where-Object Name -eq 'SHA256SUMS.txt')[0]
        $reader = [IO.StreamReader]::new($sums.Stream, [Text.Encoding]::UTF8, $true, 1024, $true)
        try { $manifest = $reader.ReadToEnd() } finally { $reader.Dispose(); $sums.Stream.Position = 0 }
        $seen = @{}
        foreach ($line in ($manifest -split '\r?\n')) {
            if (-not $line.Trim()) { continue }
            if ($line -notmatch '^([0-9a-fA-F]{64})[ \t]+\*?([^/\\]+)$') { throw 'Checksum manifest format is invalid.' }
            $digest, $name = $Matches[1].ToLowerInvariant(), $Matches[2]
            $matched = @($assets | Where-Object Name -ceq $name)
            if ($seen.ContainsKey($name) -or $matched.Count -ne 1 -or $name -eq 'SHA256SUMS.txt' -or $matched[0].Sha256 -cne $digest) { throw 'Checksum manifest does not match the release.' }
            $seen[$name] = $true
        }
        if (-not $seen.ContainsKey('XFanArtDownloader.exe')) { throw 'Checksum manifest must identify the release EXE.' }
        [pscustomobject]@{ Prefix=$script:R2ReleasePrefix; Assets=$assets }
    } catch {
        foreach ($asset in $assets) { $asset.Stream.Dispose() }
        throw 'Local release preflight failed; check the four assets and checksum manifest.'
    }
}

function Get-R2ReleaseConfiguration {
    # 只在明確發布時讀專用 DPAPI 設定；不使用 AWS/Cloudflare 環境變數。
    # Load dedicated DPAPI credentials only for explicit publishing; ignore AWS/Cloudflare environment credentials.
    $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $path = Assert-R2PublisherPlainPath (Join-Path $base 'NoonTools\Publisher\r2-credentials.clixml')
    Assert-R2PublisherPrivateAcl $path
    if (([IO.FileInfo]::new($path)).Length -gt 256KB) { throw 'Publisher configuration is invalid.' }
    $config = Import-Clixml -LiteralPath $path
    if ($config.version -ne 1 -or $config.provider -cne 'cloudflare-r2' -or $config.region -cne 'auto') { throw 'Publisher configuration is invalid.' }
    New-R2PublisherConfiguration -Endpoint $config.endpoint -Bucket $config.bucket -PublicDownloadBase $config.publicDownloadBase `
        -AccessKeyId $config.accessKeyId -SecretAccessKey $config.secretAccessKey
}

function Invoke-R2ReleaseHttp {
    param($Request)
    # 僅連官方 HTTPS endpoint，不經預設代理、不帶 OS 認證、不跟隨重新導向。
    # Contact the official HTTPS endpoint without proxies, OS credentials, or redirects.
    if ([Net.ServicePointManager]::ServerCertificateValidationCallback) { throw 'TLS validation has been overridden; use a clean PowerShell process.' }
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    $response = $null
    $web = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $web = [Net.HttpWebRequest]::Create($Request.Uri)
        $web.Method = $Request.Method
        $web.Proxy = $null
        $web.UseDefaultCredentials = $false
        $web.Credentials = $null
        $web.AllowAutoRedirect = $false
        $web.KeepAlive = $false
        $web.Timeout = 120000
        $web.ReadWriteTimeout = 45000
        foreach ($name in $Request.Headers.Keys) {
            if ($name -eq 'host') { $web.Host = $Request.Headers[$name] }
            elseif ($name -eq 'content-type') { $web.ContentType = $Request.Headers[$name] }
            else { $web.Headers[$name] = $Request.Headers[$name] }
        }
        try {
            if ($Request.Method -eq 'PUT') {
                $web.AllowWriteStreamBuffering = $false
                $web.ContentLength = $Request.Size
                $Request.Body.Position = 0
                $output = $web.GetRequestStream()
                $deadline = [Diagnostics.Stopwatch]::StartNew()
                try {
                    $buffer = [byte[]]::new(512KB)
                    while (($count = $Request.Body.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        if ($deadline.Elapsed.TotalMinutes -gt 10) { throw 'Upload timeout.' }
                        $output.Write($buffer, 0, $count)
                    }
                } finally { $output.Dispose() }
            }
            $response = $web.GetResponse()
        }
        catch [Net.WebException] {
            if ($null -eq $_.Exception.Response) { throw 'R2 network request failed.' }
            $response = $_.Exception.Response
        }
        $status = [int]$response.StatusCode
        $result = [pscustomobject]@{ Status=$status; Size=$response.ContentLength;
            Sha256=[string]$response.Headers['x-amz-meta-sha256']; ETag=[string]$response.Headers['ETag']; Digest='' }
        if ($Request.Method -eq 'GET' -and $status -eq 200) {
            $input = $response.GetResponseStream()
            $hash = [Security.Cryptography.SHA256]::Create()
            $total = 0L
            $deadline = [Diagnostics.Stopwatch]::StartNew()
            try {
                $buffer = [byte[]]::new(512KB)
                while (($count = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $count
                    if ($total -gt $Request.Size -or $deadline.Elapsed.TotalMinutes -gt 10) { throw 'Remote verification exceeded its bounds.' }
                    $null = $hash.TransformBlock($buffer, 0, $count, $buffer, 0)
                }
                $null = $hash.TransformFinalBlock([byte[]]@(), 0, 0)
                $result.Digest = ([BitConverter]::ToString($hash.Hash)).Replace('-', '').ToLowerInvariant()
                $result.Size = $total
            } finally { $input.Dispose(); $hash.Dispose() }
        }
        $result
    } catch {
        throw 'R2 HTTPS request or bounded transfer failed; no remote error payload was recorded.'
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $web) { $web.Abort() }
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
    }
}

function Invoke-R2ObjectRequest {
    param($Configuration, $Asset, [string]$Method, [hashtable]$ExtraHeaders = @{}, [scriptblock]$Transport)
    if ($Method -notin @('HEAD', 'GET', 'PUT') -or $Asset.Name -cnotin $script:R2ReleaseNames -or
        $Asset.Key -cne ($script:R2ReleasePrefix + $Asset.Name)) { throw 'Object scope is not allowed.' }
    $hostName = ([Uri]$Configuration.endpoint).DnsSafeHost
    $segments = @('noon-tools-releases') + @($Asset.Key.Split('/'))
    $canonicalPath = '/' + (($segments | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
    $hash = if ($Method -eq 'PUT') { $Asset.Sha256 } else { Get-R2HexHash ([byte[]]@()) }
    $signed = New-R2SignedHeaders -Method $Method -HostName $hostName -CanonicalPath $canonicalPath -PayloadHash $hash `
        -Headers $ExtraHeaders -AccessKeyId $Configuration.accessKeyId -SecretAccessKey $Configuration.secretAccessKey
    $request = [pscustomobject]@{ Method=$Method; Uri=$Configuration.endpoint+$canonicalPath;
        Headers=$signed.Headers; Body=$Asset.Stream; Size=$Asset.Size }
    & $Transport $request
}

function Assert-R2RemoteAsset {
    param($Configuration, $Asset, $Head, [scriptblock]$Transport)
    # metadata 與大小仍不足以略過；完整下載雜湊再次確認，ETag 僅作並行條件。
    # Metadata and size alone never justify a skip; verify the full download, using ETag only as a concurrency condition.
    if ($Head.Status -ne 200) { throw ('R2_HEAD_HTTP_' + [int]$Head.Status) }
    if ($Head.Size -ne $Asset.Size -or $Head.Sha256 -cne $Asset.Sha256) { throw 'Existing object conflicts with the local release; overwrite is forbidden.' }
    $headers = @{}
    if ($Head.ETag) { $headers['if-match'] = $Head.ETag }
    $get = Invoke-R2ObjectRequest $Configuration $Asset 'GET' $headers $Transport
    if ($get.Status -ne 200) { throw ('R2_GET_HTTP_' + [int]$get.Status) }
    if ($get.Size -ne $Asset.Size -or $get.Digest -cne $Asset.Sha256) { throw 'Remote object content could not be verified; overwrite is forbidden.' }
}

function Invoke-R2ReleasePublish {
    param($Plan, $Configuration, [scriptblock]$Transport = { param($Request) Invoke-R2ReleaseHttp $Request },
          [hashtable]$States = @{})
    $config = New-R2PublisherConfiguration -Endpoint $Configuration.endpoint -Bucket $Configuration.bucket `
        -PublicDownloadBase $Configuration.publicDownloadBase -AccessKeyId $Configuration.accessKeyId -SecretAccessKey $Configuration.secretAccessKey
    if ($Plan.Prefix -cne $script:R2ReleasePrefix -or $Plan.Assets.Count -ne 4 -or
        @($Plan.Assets.Name | Sort-Object -Unique).Count -ne 4) { throw 'Release plan scope is invalid.' }
    # 先檢查全部物件，任何衝突都在第一個 PUT 之前停止。
    # Preflight every object; any existing conflict stops the release before the first PUT.
    foreach ($asset in $Plan.Assets) {
        $head = Invoke-R2ObjectRequest $config $asset 'HEAD' @{} $Transport
        if ($head.Status -eq 404) { $states[$asset.Name] = 'missing' }
        elseif ($head.Status -eq 200) { Assert-R2RemoteAsset $config $asset $head $Transport; $states[$asset.Name] = 'existing_verified' }
        else { throw ('R2_HEAD_HTTP_' + [int]$head.Status) }
    }
    foreach ($asset in $Plan.Assets) {
        if ($states[$asset.Name] -ne 'missing') { continue }
        # If-None-Match 也納入簽章；原子條件阻止預檢後的競爭覆寫。
        # Sign If-None-Match too; its atomic precondition prevents races after preflight.
        $headers = @{'if-none-match'='*'; 'x-amz-meta-sha256'=$asset.Sha256;
            'content-type'=$asset.ContentType; 'content-disposition'=('attachment; filename="' + $asset.Name + '"');
            'cache-control'='public, max-age=31536000, immutable'}
        $states[$asset.Name] = 'upload_unverified'
        $put = Invoke-R2ObjectRequest $config $asset 'PUT' $headers $Transport
        if ($put.Status -notin @(200, 201, 412)) { throw ('R2_PUT_HTTP_' + [int]$put.Status) }
        $head = Invoke-R2ObjectRequest $config $asset 'HEAD' @{} $Transport
        Assert-R2RemoteAsset $config $asset $head $Transport
        $states[$asset.Name] = if ($put.Status -eq 412) { 'existing_verified' } else { 'uploaded_verified' }
    }
    $states
}

function Write-R2ReleaseReport {
    param([string]$Path, $Report)
    $target = Assert-R2PublisherPlainPath ([IO.Path]::GetFullPath($Path))
    if ([IO.Path]::GetExtension($target) -cne '.json') { throw 'The nonsecret report must use a .json filename.' }
    $parent = [IO.Path]::GetDirectoryName($target)
    if (-not [IO.Directory]::Exists($parent)) { throw 'Report directory must already exist.' }
    $temporary = Join-Path $parent ('.r2-report.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($Report | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
        $null = Assert-R2PublisherPlainPath $target
        if ([IO.File]::Exists($target)) { [IO.File]::Replace($temporary, $target, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $target) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Invoke-R2ReleaseMain {
    $plan = $config = $null
    $states = @{}
    $publicBase = ''
    $report = [ordered]@{ version=1; bucket='noon-tools-releases'; prefix=$script:R2ReleasePrefix;
        publishRequested=[bool]$Publish; status='failed'; createdAtUtc=[DateTime]::UtcNow.ToString('o'); assets=@() }
    try {
        if (-not $AssetDirectory -or -not $ReportPath) { throw 'AssetDirectory and ReportPath are required.' }
        if ($PublicDownloadBase -and $PublicDownloadBase.TrimEnd('/') -cne 'https://downloads.noonisawesome.dev') { throw 'Only the dedicated public download domain is allowed.' }
        $plan = New-R2ReleasePlan -Directory $AssetDirectory
        foreach ($asset in $plan.Assets) { $states[$asset.Name] = 'local_verified' }
        $publicBase = $PublicDownloadBase.TrimEnd('/')
        $report.assets = @(Get-R2ReportAssets $plan $states $publicBase)
        Write-R2ReleaseReport -Path $ReportPath -Report $report
        if ($Publish) {
            $config = Get-R2ReleaseConfiguration
            $states = Invoke-R2ReleasePublish -Plan $plan -Configuration $config -States $states
        }
        if (-not $publicBase -and $config) { $publicBase = $config.publicDownloadBase }
        $report.assets = @(Get-R2ReportAssets $plan $states $publicBase)
        $report.status = if ($Publish) { 'verified' } else { 'local_plan_only' }
        Write-R2ReleaseReport -Path $ReportPath -Report $report
        Write-Host ('Release result: ' + $report.status + '. Nonsecret report written.')
        if (-not $Publish) { Write-Host 'Dry run: no credential file was read and no network request was made.' }
    } catch {
        # 不輸出原始例外、HTTP 內容或 Authorization，報告亦只寫固定結果。
        # Never emit raw exceptions, HTTP payloads, or Authorization; reports contain fixed public outcomes only.
        $report['errorCode'] = 'release_failed'
        if ($_.Exception.Message -cmatch '^R2_(HEAD|GET|PUT)_HTTP_[1-5][0-9]{2}$') { $report['errorCode'] = $_.Exception.Message }
        if ($plan) { $report.assets = @(Get-R2ReportAssets $plan $states $publicBase) }
        if ($ReportPath) { try { Write-R2ReleaseReport -Path $ReportPath -Report $report } catch { } }
        Write-Host 'Release stopped. Check local assets, private R2 configuration, connectivity, and existing-object conflicts. No overwrite or delete was requested.'
        Write-Host ('Result code: ' + $report['errorCode'])
        exit 1
    } finally {
        if ($plan) { foreach ($asset in $plan.Assets) { $asset.Stream.Dispose() } }
        if ($config) { $config.accessKeyId.Dispose(); $config.secretAccessKey.Dispose() }
    }
}

function Get-R2ReportAssets($Plan, [hashtable]$States, [string]$PublicBase) {
    # 報告僅含公開資產與固定狀態；失敗也保留已驗證／尚未驗證的進度。
    # Reports contain public assets and fixed states only, retaining verified/unverified progress on failure.
    foreach ($asset in $Plan.Assets) {
        [ordered]@{name=$asset.Name; key=$asset.Key; size=$asset.Size; sha256=$asset.Sha256;
            state=$States[$asset.Name]; publicUrl=$(if ($PublicBase) { $PublicBase + '/' + $asset.Key } else { '' })}
    }
}

if (-not $LibraryOnly) { Invoke-R2ReleaseMain }
