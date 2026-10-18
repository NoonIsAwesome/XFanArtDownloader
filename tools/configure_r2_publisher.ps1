#requires -Version 5.1
[CmdletBinding()]
param([switch]$LibraryOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-R2PublisherConfiguration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Endpoint,
        [string]$Bucket = 'noon-tools-releases',
        [AllowEmptyString()][string]$PublicDownloadBase = '',
        [Parameter(Mandatory=$true)][Security.SecureString]$AccessKeyId,
        [Parameter(Mandatory=$true)][Security.SecureString]$SecretAccessKey
    )
    # 限定專用 bucket 與官方 S3 endpoint，避免碰到既有更新服務。
    # Pin the dedicated bucket and official S3 endpoint; never reuse the existing update service.
    $endpointValue = $Endpoint.Trim()
    if ($endpointValue -notmatch '^https://[0-9a-f]{32}(\.(eu|us|fedramp))?\.r2\.cloudflarestorage\.com/?$') {
        throw 'R2 endpoint must be HTTPS with a 32-character account ID and an optional eu/us/fedramp jurisdiction.'
    }
    if ($Bucket.Trim() -cne 'noon-tools-releases') {
        throw 'Use only the dedicated noon-tools-releases bucket; existing update buckets are not allowed.'
    }
    $publicValue = $PublicDownloadBase.Trim().TrimEnd('/')
    if ($publicValue) {
        $publicUri = $null
        if (-not [Uri]::TryCreate($publicValue, [UriKind]::Absolute, [ref]$publicUri) -or
            $publicValue -notmatch '^https://[a-z0-9.-]+(/[a-z0-9/_.~-]*)?$' -or
            $publicUri.HostNameType -ne [UriHostNameType]::Dns -or
            $publicUri.DnsSafeHost -notmatch '^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}[a-z]{2,63}$' -or
            $publicUri.DnsSafeHost -eq 'updates.noonisawesome.dev' -or
            $publicUri.DnsSafeHost.EndsWith('.updates.noonisawesome.dev') -or
            $publicValue -match '(^|/)\.\.?(/|$)') {
            throw 'Public download base must use a separate HTTPS subdomain without credentials, ports, query strings, or traversal.'
        }
        $publicValue = $publicUri.AbsoluteUri.TrimEnd('/')
    }
    # 金鑰全程保留為 SecureString，不轉為字串、不讀取環境中的舊 token。
    # Keep both keys as SecureString; never convert them to text or read old environment tokens.
    if ($AccessKeyId.Length -lt 16 -or $AccessKeyId.Length -gt 1024 -or
        $SecretAccessKey.Length -lt 16 -or $SecretAccessKey.Length -gt 1024) {
        throw 'Both R2 S3 keys must be nonempty SecureString values of a reasonable length.'
    }
    [pscustomobject]@{
        version = 1
        provider = 'cloudflare-r2'
        endpoint = $endpointValue.ToLowerInvariant().TrimEnd('/')
        bucket = 'noon-tools-releases'
        region = 'auto'
        publicDownloadBase = $publicValue
        accessKeyId = $AccessKeyId
        secretAccessKey = $SecretAccessKey
        createdAtUtc = [DateTime]::UtcNow.ToString('o')
    }
}

function Assert-R2PublisherPlainPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    # 檢查每一層既有路徑，拒絕 symlink、junction 與其他 reparse point。
    # Inspect every existing ancestor and reject symlinks, junctions, and all reparse points.
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[a-z]:\\' -or -not [IO.Path]::IsPathRooted($Path) -or
        $Path -match '(^|[\\/])\.\.([\\/]|$)' -or
        [IO.DriveInfo]::new([IO.Path]::GetPathRoot($full)).DriveType -eq [IO.DriveType]::Network) {
        throw 'The publisher configuration path must be an absolute local path without traversal.'
    }
    $current = $full
    while ($current) {
        if ([IO.File]::Exists($current) -or [IO.Directory]::Exists($current)) {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'The publisher configuration path contains a link or reparse point.'
            }
        } else {
            # GetAttributes 也能發現指向不存在目標的連結；只有真的不存在才略過。
            # GetAttributes also detects dangling links; skip only genuinely missing components.
            try {
                $attributes = [IO.File]::GetAttributes($current)
                if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw 'The publisher configuration path contains a link or reparse point.'
                }
            } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
    $full
}

function Set-R2PublisherPrivateAcl {
    param([Parameter(Mandatory=$true)][string]$Path, [switch]$Directory)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    if ($Directory) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetOwner($identity)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($identity, $system)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
            [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) }
    else { [IO.File]::SetAccessControl($Path, $acl) }
}

function Assert-R2PublisherPrivateAcl {
    param([Parameter(Mandatory=$true)][string]$Path)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid) {
        throw 'The existing publisher configuration does not have the required private ownership and ACL.'
    }
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 2) { throw 'The publisher configuration ACL must contain only the current user and SYSTEM.' }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin @($sid, 'S-1-5-18') -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) {
            throw 'The publisher configuration ACL must contain only the current user and SYSTEM.'
        }
    }
}

function Save-R2PublisherConfiguration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)]$Configuration,
        [switch]$Confirmed,
        [string]$TestDirectory
    )
    if (-not $Confirmed) { return [pscustomobject]@{ Saved = $false; Path = $null } }
    if ($env:OS -ne 'Windows_NT') { throw 'This configuration helper requires Windows DPAPI.' }
    $validated = New-R2PublisherConfiguration -Endpoint $Configuration.endpoint -Bucket $Configuration.bucket `
        -PublicDownloadBase $Configuration.publicDownloadBase -AccessKeyId $Configuration.accessKeyId `
        -SecretAccessKey $Configuration.secretAccessKey
    if ($TestDirectory) {
        # 測試只能使用明確指定的 TEMP 子目錄，不能導向真人設定檔。
        # Tests may target an explicit TEMP child directory only, never a real user configuration.
        $base = Assert-R2PublisherPlainPath $TestDirectory
        $tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $base.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
            -not [IO.Directory]::Exists($base)) {
            throw 'The test directory must be an existing child of the Windows TEMP directory.'
        }
    } else {
        $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
        if (-not $base) { throw 'The current user LocalAppData directory is unavailable.' }
    }
    $directory = Join-Path (Join-Path $base 'NoonTools') 'Publisher'
    $destination = Join-Path $directory 'r2-credentials.clixml'
    $null = Assert-R2PublisherPlainPath $destination
    if ([IO.File]::Exists($destination)) { Assert-R2PublisherPrivateAcl $destination }
    $null = [IO.Directory]::CreateDirectory($directory)
    $null = Assert-R2PublisherPlainPath $directory
    Set-R2PublisherPrivateAcl -Path $directory -Directory
    Assert-R2PublisherPrivateAcl $directory
    $temporary = Join-Path $directory ('r2-credentials.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        # 先在受限目錄建立加密暫存檔；DPAPI 解密驗證成功後才原子取代舊設定。
        # Export encrypted data inside the private directory, verify DPAPI, then atomically replace the old file.
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Dispose()
        Set-R2PublisherPrivateAcl $temporary
        $validated | Export-Clixml -LiteralPath $temporary -Depth 4 -Encoding UTF8
        Assert-R2PublisherPrivateAcl $temporary
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Flush($true) } finally { $stream.Dispose() }
        $roundtrip = Import-Clixml -LiteralPath $temporary
        if ($roundtrip.accessKeyId -isnot [Security.SecureString] -or
            $roundtrip.secretAccessKey -isnot [Security.SecureString] -or
            $roundtrip.accessKeyId.Length -ne $validated.accessKeyId.Length -or
            $roundtrip.secretAccessKey.Length -ne $validated.secretAccessKey.Length) {
            throw 'The encrypted publisher configuration failed its local DPAPI verification.'
        }
        $roundtrip.accessKeyId.Dispose()
        $roundtrip.secretAccessKey.Dispose()
        $null = Assert-R2PublisherPlainPath $destination
        if ([IO.File]::Exists($destination)) {
            Assert-R2PublisherPrivateAcl $destination
            [IO.File]::Replace($temporary, $destination, [NullString]::Value)
        } else {
            [IO.File]::Move($temporary, $destination)
        }
        [pscustomobject]@{ Saved = $true; Path = $destination }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Invoke-R2PublisherSetup {
    $access = $null
    $secret = $null
    try {
        Write-Host 'NoonToolsPublisher - local encrypted R2 configuration'
        Write-Host 'Use Object Read & Write scoped ONLY to noon-tools-releases. Never paste keys into chat.'
        Write-Host 'This tool does not upload, list objects, change DNS, or configure a public domain.'
        $endpoint = Read-Host 'S3 endpoint (https://ACCOUNT_ID.r2.cloudflarestorage.com)'
        $bucket = Read-Host 'Dedicated bucket [noon-tools-releases]'
        if (-not $bucket) { $bucket = 'noon-tools-releases' }
        $public = Read-Host 'Public HTTPS base [https://downloads.noonisawesome.dev], or - to leave empty'
        if (-not $public) { $public = 'https://downloads.noonisawesome.dev' }
        if ($public -eq '-') { $public = '' }
        $access = Read-Host 'R2 S3 Access Key ID (hidden)' -AsSecureString
        $secret = Read-Host 'R2 S3 Secret Access Key (hidden)' -AsSecureString
        $configuration = New-R2PublisherConfiguration -Endpoint $endpoint -Bucket $bucket `
            -PublicDownloadBase $public -AccessKeyId $access -SecretAccessKey $secret
        $answer = Read-Host 'Save encrypted configuration for this Windows user? [y/N]'
        if ($answer -ine 'y') { Write-Host 'Cancelled. Existing configuration was preserved.'; return }
        $result = Save-R2PublisherConfiguration -Configuration $configuration -Confirmed
        Write-Host ('Saved locally: ' + $result.Path)
        Write-Host 'Both keys are DPAPI-encrypted for this Windows user and computer. No network request was made.'
    } catch {
        # 僅顯示固定錯誤；不要把輸入物件、例外內容或金鑰印到終端。
        # Display a fixed failure only; never print input objects, exception payloads, or keys.
        Write-Host 'Configuration was not saved. Check the endpoint, dedicated bucket, HTTPS base, and private local path.'
        Write-Host 'If another process is using the file, close it and retry. Do not share credential files.'
        exit 1
    } finally {
        if ($null -ne $access) { $access.Dispose() }
        if ($null -ne $secret) { $secret.Dispose() }
    }
}

if (-not $LibraryOnly) { Invoke-R2PublisherSetup }
