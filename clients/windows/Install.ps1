param([Parameter(Mandatory=$true)][string]$PayloadPath, [switch]$SelfTest, [string]$TestRoot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (!$SelfTest) { Add-Type -AssemblyName System.Windows.Forms }

function Assert-OwnedDirectory([string]$Directory) {
    $marker = Join-Path $Directory 'install-manifest.json'
    if ((Get-Item -LiteralPath $Directory).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked destination refused.' }
    if (!(Test-Path -LiteralPath $marker -PathType Leaf)) { throw 'Destination has no owned installation marker.' }
    $manifest = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($manifest.app_id -ne 'com.sina.oco.viewer') { throw 'Unrelated destination. Installation stopped.' }
    $allowed = @($manifest.files) + @('install-manifest.json')
    foreach ($item in Get-ChildItem -LiteralPath $Directory -Force) {
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $allowed -notcontains $item.Name) {
            throw 'Destination contains unrelated files or links. Move those files aside before updating.'
        }
    }
}

function Install-Viewer([string]$Destination, [switch]$InjectFailure) {
    $stage = $Destination + '.stage-' + [Guid]::NewGuid().ToString('N')
    $backup = $Destination + '.backup-' + [Guid]::NewGuid().ToString('N')
    $oldMoved = $false
    $newMoved = $false
    try {
        if (Test-Path -LiteralPath $Destination) { Assert-OwnedDirectory $Destination }
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        $archive = [IO.Compression.ZipFile]::OpenRead($PayloadPath)
        $installed = @()
        try {
            foreach ($entry in $archive.Entries) {
                $name = $entry.FullName
                if (!$name -or $name -in @('.', '..', 'install-manifest.json', 'Open-SinaOCO.cmd') -or $name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $installed -contains $name) { throw 'Unexpected archive path or duplicate.' }
                $inputStream = $entry.Open()
                try {
                    $outputStream = [IO.File]::Open((Join-Path $stage $name), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                    try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
                } finally { $inputStream.Dispose() }
                $installed += $name
            }
        } finally { $archive.Dispose() }
        [IO.File]::WriteAllText((Join-Path $stage 'Open-SinaOCO.cmd'), "@echo off`r`nstart `"`" `"%~dp0index.html`"`r`n", [Text.Encoding]::ASCII)
        $installed += 'Open-SinaOCO.cmd'
        @{app_id='com.sina.oco.viewer'; version='0.4.1'; files=$installed} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $stage 'install-manifest.json') -Encoding UTF8
        if (Test-Path -LiteralPath $Destination) { [IO.Directory]::Move($Destination, $backup); $oldMoved = $true }
        if ($InjectFailure) { throw 'Injected rollback test.' }
        [IO.Directory]::Move($stage, $Destination); $newMoved = $true
    } catch {
        if ($newMoved -and (Test-Path -LiteralPath $Destination)) { Assert-OwnedDirectory $Destination; Remove-Item -LiteralPath $Destination -Recurse -Force }
        if ($oldMoved) { [IO.Directory]::Move($backup, $Destination) }
        throw
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    }
    if ($oldMoved) { Assert-OwnedDirectory $backup; Remove-Item -LiteralPath $backup -Recurse -Force }
}

try {
    if ((Get-FileHash -LiteralPath $PayloadPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne '$EXPECTED_SHA256') { throw 'Installer payload integrity check failed.' }
    if ($SelfTest) {
        # The bootstrap supplies a unique temporary directory. Never test against AppData.
        $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (!$TestRoot -or ![IO.Path]::GetFullPath($TestRoot).StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $TestRoot)) { throw 'Self-test requires a new temporary destination.' }
        try {
            Install-Viewer $TestRoot
            Assert-OwnedDirectory $TestRoot
            $before = (Get-FileHash -LiteralPath (Join-Path $TestRoot 'index.html') -Algorithm SHA256).Hash
            $rolledBack = $false
            try { Install-Viewer $TestRoot -InjectFailure } catch { if ($_.Exception.Message -ne 'Injected rollback test.') { throw }; $rolledBack = $true }
            if (!$rolledBack -or (Get-FileHash -LiteralPath (Join-Path $TestRoot 'index.html') -Algorithm SHA256).Hash -ne $before) { throw 'Rollback test failed.' }
            Install-Viewer $TestRoot
            Assert-OwnedDirectory $TestRoot
            if (!(Test-Path -LiteralPath (Join-Path $TestRoot 'Uninstall.ps1'))) { throw 'Removal tool missing.' }
            [IO.File]::WriteAllText((Join-Path $TestRoot 'unrelated-test.txt'), 'keep')
            $refused = $false
            try { Install-Viewer $TestRoot } catch { $refused = $true }
            if (!$refused -or (Get-Content -LiteralPath (Join-Path $TestRoot 'unrelated-test.txt') -Raw) -ne 'keep') { throw 'Unrelated-file protection failed.' }
            Remove-Item -LiteralPath (Join-Path $TestRoot 'unrelated-test.txt')
            exit 0
        } finally { if (Test-Path -LiteralPath $TestRoot) { Assert-OwnedDirectory $TestRoot; Remove-Item -LiteralPath $TestRoot -Recurse -Force } }
    }
    $root = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SinaOCO'
    Install-Viewer $root
    $launch = Join-Path $root 'Open-SinaOCO.cmd'
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'Sina OCO Viewer.lnk'
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -ne $launch) { throw 'Unrelated Start-menu shortcut exists; viewer was installed but shortcut was preserved.' }
    $shortcut.TargetPath = $launch; $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'Sina OCO research viewer; live remains disabled'; $shortcut.Save()
    [System.Windows.Forms.MessageBox]::Show('Viewer installed. Open a saved report or your own HTTPS server. No cloud or live signal was activated.', 'Sina OCO', 'OK', 'Information') | Out-Null
    Start-Process -FilePath $launch
    exit 0
} catch {
    if ($SelfTest -and $env:SINA_OCO_SELF_TEST_LOG) { [IO.File]::WriteAllText($env:SINA_OCO_SELF_TEST_LOG, 'FAILED: ' + $_.Exception.Message, [Text.Encoding]::UTF8) }
    if (!$SelfTest) { [System.Windows.Forms.MessageBox]::Show('Installation did not complete: ' + $_.Exception.Message, 'Sina OCO', 'OK', 'Error') | Out-Null }
    exit 1
}
