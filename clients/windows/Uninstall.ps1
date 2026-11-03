$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$root = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SinaOCO'
$marker = Join-Path $root 'install-manifest.json'
if (!(Test-Path -LiteralPath $marker)) { throw 'Owned installation marker not found.' }
$manifest = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
if ($manifest.app_id -ne 'com.sina.oco.viewer') { throw 'Unrelated installation. Refusing removal.' }
if ([System.Windows.Forms.MessageBox]::Show('Remove Sina OCO Viewer for this user?', 'Sina OCO', 'YesNo', 'Question') -ne 'Yes') { exit 0 }
$prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
foreach ($name in $manifest.files) {
    if ($name.Contains('/') -or $name.Contains('\') -or [IO.Path]::IsPathRooted($name)) { throw 'Invalid owned-file entry.' }
    $target = [IO.Path]::GetFullPath((Join-Path $root $name))
    if (!$target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Removal path rejected.' }
    Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
}
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Sina OCO Viewer.lnk'
if (Test-Path -LiteralPath $shortcut) {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    if ($link.TargetPath -eq (Join-Path $root 'Open-SinaOCO.cmd')) { Remove-Item -LiteralPath $shortcut -Force }
}
Remove-Item -LiteralPath $marker -Force
if (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 0) { Remove-Item -LiteralPath $root }
[System.Windows.Forms.MessageBox]::Show('Sina OCO Viewer was removed. Other files and browser data were preserved.', 'Sina OCO', 'OK', 'Information') | Out-Null
