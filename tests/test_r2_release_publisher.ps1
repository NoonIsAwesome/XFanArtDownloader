#requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\tools\publish_r2_release.ps1') -LibraryOnly

function Assert-Publisher($Condition, [string]$Description) {
    if (-not $Condition) { throw ('Synthetic publisher assertion failed: ' + $Description) }
}
function Assert-PublisherRejected([scriptblock]$Action, [string]$Description) {
    $rejected = $false
    try { $null = & $Action } catch { $rejected = $true }
    Assert-Publisher $rejected $Description
}
function New-FakeTransport($State) {
    {
        param($Request)
        $name = [Uri]::UnescapeDataString(([Uri]$Request.Uri).AbsolutePath.Split('/')[-1])
        $State.Calls.Add(($Request.Method + ':' + $name))
        $response = [pscustomobject]@{Status=404; Size=0; Sha256=''; Digest=''; ETag='"fixture"'}
        if ($Request.Method -eq 'HEAD') {
            if ($State.Redirect -eq $name) { $response.Status=301; return $response }
            if ($State.Objects.ContainsKey($name)) {
                $object = $State.Objects[$name]
                $response.Status=200; $response.Size=$object.Bytes.Length; $response.Sha256=$object.Sha256
            }
        } elseif ($Request.Method -eq 'GET') {
            $object = $State.Objects[$name]
            $response.Status=200; $response.Size=$object.Bytes.Length; $response.Digest=Get-R2HexHash $object.Bytes
        } elseif ($Request.Method -eq 'PUT') {
            Assert-Publisher ($Request.Headers['if-none-match'] -ceq '*') 'conditional PUT is mandatory'
            Assert-Publisher ($Request.Headers['authorization'] -match 'SignedHeaders=[^,]*if-none-match') 'conditional header is signed'
            Assert-Publisher ($Request.Headers['cache-control'] -match 'immutable') 'release cache is immutable'
            if ($State.FailPut -eq $name) { $response.Status=500; return $response }
            if ($State.Race -eq $name) {
                $bytes = if ($State.RaceConflict) { [Text.Encoding]::UTF8.GetBytes('competing different release') } else { $State.Local[$name] }
                $State.Objects[$name] = @{Bytes=$bytes; Sha256=(Get-R2HexHash $bytes)}
            }
            if ($State.Objects.ContainsKey($name)) { $response.Status=412; return $response }
            $memory = [IO.MemoryStream]::new()
            try { $Request.Body.Position=0; $Request.Body.CopyTo($memory); $bytes=$memory.ToArray() } finally { $memory.Dispose() }
            $State.Objects[$name] = @{Bytes=$bytes; Sha256=$Request.Headers['x-amz-meta-sha256']}
            $response.Status=200
        } else { throw 'Unexpected request method in synthetic fixture.' }
        $response
    }.GetNewClosure()
}
function New-FakeState($Plan) {
    $state = @{Objects=@{}; Calls=[Collections.Generic.List[string]]::new(); Redirect=''; Race=''; RaceConflict=$false; FailPut=''; Local=@{}}
    foreach ($asset in $Plan.Assets) {
        $memory = [IO.MemoryStream]::new()
        try { $asset.Stream.Position=0; $asset.Stream.CopyTo($memory); $state.Local[$asset.Name]=$memory.ToArray(); $asset.Stream.Position=0 }
        finally { $memory.Dispose() }
    }
    $state
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('NoonPublisherUploadTest-' + [Guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($root)
$plan = $null
$access = ConvertTo-SecureString 'AKIAIOSFODNN7EXAMPLE' -AsPlainText -Force
$secret = ConvertTo-SecureString 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY' -AsPlainText -Force
$checks = 0
try {
    # 官方 AWS 公開範例向量，沒有真人帳密或網路。
    # Verify the official public AWS test vector without real credentials or network access.
    $empty = Get-R2HexHash ([byte[]]@())
    $signed = New-R2SignedHeaders -Method GET -HostName 'examplebucket.s3.amazonaws.com' -CanonicalPath '/test.txt' `
        -PayloadHash $empty -Headers @{'range'='bytes=0-9'} -AccessKeyId $access -SecretAccessKey $secret `
        -Now ([DateTime]::Parse('2013-05-24T00:00:00Z').ToUniversalTime()) -Region 'us-east-1'
    Assert-Publisher ($signed.CanonicalHash -ceq '7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972') 'AWS canonical request hash'
    Assert-Publisher ($signed.Signature -ceq 'f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41') 'AWS signature reference'
    $checks++
    $exe = [Text.Encoding]::UTF8.GetBytes('MZ' + ('synthetic-only-' * 20))
    [IO.File]::WriteAllBytes((Join-Path $root 'XFanArtDownloader.exe'), $exe)
    [IO.File]::WriteAllText((Join-Path $root 'LICENSE'), 'synthetic GPL license fixture')
    [IO.File]::WriteAllText((Join-Path $root 'THIRD_PARTY_NOTICES.txt'), 'synthetic notices fixture')
    $exeSha = Get-R2HexHash $exe
    [IO.File]::WriteAllText((Join-Path $root 'SHA256SUMS.txt'), $exeSha + '  XFanArtDownloader.exe')
    $plan = New-R2ReleasePlan -Directory $root -ExpectedExeSha256 $exeSha
    Assert-Publisher ($plan.Assets.Count -eq 4) 'four assets only'
    Assert-PublisherRejected { [IO.File]::Open((Join-Path $root 'XFanArtDownloader.exe'), 'Open', 'Write', 'Read').Dispose() } 'asset read lock blocks modification'
    $checks++
    $configuration = New-R2PublisherConfiguration -Endpoint ('https://' + ('1' * 32) + '.r2.cloudflarestorage.com') `
        -AccessKeyId $access -SecretAccessKey $secret
    $state = New-FakeState $plan
    $transport = New-FakeTransport $state
    $states = Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport
    Assert-Publisher (@($states.Values | Where-Object { $_ -ne 'uploaded_verified' }).Count -eq 0) 'four uploads verified'
    Assert-Publisher (@($state.Calls[0..3] | Where-Object { $_ -notlike 'HEAD:*' }).Count -eq 0) 'all-object preflight before upload'
    Assert-Publisher (@($state.Calls | Where-Object { $_ -like 'PUT:*' }).Count -eq 4) 'exactly four PUTs'
    $checks++
    $state.Calls.Clear()
    $states = Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport
    Assert-Publisher (@($states.Values | Where-Object { $_ -ne 'existing_verified' }).Count -eq 0) 'identical objects safely skipped'
    Assert-Publisher (@($state.Calls | Where-Object { $_ -like 'PUT:*' }).Count -eq 0) 'rerun never overwrites'
    $checks++
    foreach ($conflict in @('metadata', 'content', 'size')) {
        $state = New-FakeState $plan
        $asset = $plan.Assets[-1]
        $bytes = if ($conflict -eq 'size') { [byte[]]@(1,2,3) } else { [byte[]]$state.Local[$asset.Name].Clone() }
        if ($conflict -eq 'content') { $bytes[0] = $bytes[0] -bxor 1 }
        $sha = if ($conflict -eq 'metadata') { '0' * 64 } else { $asset.Sha256 }
        $state.Objects[$asset.Name]=@{Bytes=$bytes; Sha256=$sha}
        $transport = New-FakeTransport $state
        Assert-PublisherRejected { Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport } 'existing conflict stops all uploads'
        Assert-Publisher (@($state.Calls | Where-Object { $_ -like 'PUT:*' }).Count -eq 0) 'conflict never uploads earlier absent assets'
    }
    $checks++
    foreach ($different in @($false, $true)) {
        $state = New-FakeState $plan
        $state.Race='XFanArtDownloader.exe'; $state.RaceConflict=$different
        $transport = New-FakeTransport $state
        if ($different) {
            Assert-PublisherRejected { Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport } '412 conflict rejected'
            Assert-Publisher ((Get-R2HexHash $state.Objects[$state.Race].Bytes) -cne $exeSha) 'competitor object preserved'
        } else {
            $states = Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport
            Assert-Publisher ($states['XFanArtDownloader.exe'] -ceq 'existing_verified') '412 identical object verified before skip'
        }
    }
    $checks++
    $state = New-FakeState $plan; $state.Redirect='XFanArtDownloader.exe'; $transport=New-FakeTransport $state
    Assert-PublisherRejected { Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport } 'redirect status rejected'
    Assert-Publisher (@($state.Calls | Where-Object { $_ -like 'PUT:*' }).Count -eq 0) 'redirect never uploads'
    $checks++
    $bad = $configuration.PSObject.Copy(); $bad.bucket='singing-stream-savior-updates'
    $state.Calls.Clear()
    Assert-PublisherRejected { Invoke-R2ReleasePublish -Plan $plan -Configuration $bad -Transport $transport } 'old bucket prohibited'
    Assert-Publisher ($state.Calls.Count -eq 0) 'invalid configuration never requests'
    $checks++
    $state = New-FakeState $plan; $state.FailPut='SHA256SUMS.txt'; $transport=New-FakeTransport $state
    $partial = @{}
    Assert-PublisherRejected { Invoke-R2ReleasePublish -Plan $plan -Configuration $configuration -Transport $transport -States $partial } 'later upload failure is reported'
    Assert-Publisher ($partial['XFanArtDownloader.exe'] -ceq 'uploaded_verified') 'earlier verified upload retained in report state'
    Assert-Publisher ($partial['SHA256SUMS.txt'] -ceq 'upload_unverified') 'failed upload is not falsely complete'
    Assert-Publisher ($partial['LICENSE'] -ceq 'missing') 'unattempted asset remains absent'
    $checks++
    $reportPath = Join-Path $root 'report.json'
    Write-R2ReleaseReport $reportPath @{status='fixture'; assets=@(@{name='LICENSE'; sha256=('0'*64)})}
    $report = [IO.File]::ReadAllText($reportPath)
    Assert-Publisher (-not $report.Contains('AKIAIOSFODNN7EXAMPLE') -and -not $report.Contains('wJalrXUtnFEMI')) 'report has no synthetic key material'
    $checks++
    $plan.Assets | ForEach-Object { $_.Stream.Dispose() }; $plan=$null
    [IO.File]::WriteAllText((Join-Path $root 'SHA256SUMS.txt'), ('0' * 64) + '  XFanArtDownloader.exe')
    Assert-PublisherRejected { New-R2ReleasePlan -Directory $root -ExpectedExeSha256 $exeSha } 'bad manifest rejected'
    $checks++
    Write-Output ('PASS: ' + $checks + ' synthetic publisher checks; zero real credentials or network requests.')
} finally {
    if ($plan) { foreach ($asset in $plan.Assets) { $asset.Stream.Dispose() } }
    $access.Dispose(); $secret.Dispose()
    # 僅清除此合成 TEMP 子目錄，先驗證絕對路徑與專用名稱。
    # Remove only this synthetic TEMP child after checking its absolute boundary and dedicated name.
    $full = [IO.Path]::GetFullPath($root)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($full.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($full).StartsWith('NoonPublisherUploadTest-')) { Remove-Item -LiteralPath $full -Recurse -Force }
}
