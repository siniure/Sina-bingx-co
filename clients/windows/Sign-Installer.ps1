param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$CertificateThumbprint,
    [Parameter(Mandatory=$true)][uri]$TimestampUrl
)
# Run on the publisher's Windows signing machine. No PFX password or private key
# is accepted as a parameter. The certificate must already be in CurrentUser\My.
$ErrorActionPreference = 'Stop'
if ($TimestampUrl.Scheme -notin @('http', 'https') -or $TimestampUrl.UserInfo) { throw 'Use the public RFC3161 timestamp URL supplied by the certificate authority.' }
$target = (Resolve-Path -LiteralPath $InstallerPath).Path
if ([IO.Path]::GetExtension($target) -ne '.exe') { throw 'Expected the final installer EXE.' }
$certificate = Get-Item -LiteralPath ('Cert:\CurrentUser\My\' + $CertificateThumbprint)
if (!$certificate.HasPrivateKey -or $certificate.NotAfter -le (Get-Date)) { throw 'An unexpired publisher certificate with its own private key is required.' }
if (@(Get-ChildItem -Path Cert:\CurrentUser\My -CodeSigningCert).Thumbprint -notcontains $CertificateThumbprint) { throw 'Certificate does not allow code signing.' }
$chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
try { if (!$chain.Build($certificate)) { throw 'Publisher certificate chain is not trusted on this machine.' } } finally { $chain.Dispose() }
$tool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (!$tool) { throw 'Install Windows SDK Signing Tools and run from a developer prompt with signtool.exe in PATH.' }
& $tool.Source sign /s My /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl.AbsoluteUri /td SHA256 $target
if ($LASTEXITCODE -ne 0) { throw 'Publisher signing failed.' }
& $tool.Source verify /pa /all /v $target
if ($LASTEXITCODE -ne 0 -or (Get-AuthenticodeSignature -LiteralPath $target).Status -ne 'Valid') { throw 'Signature verification failed.' }
@{artifact=[IO.Path]::GetFileName($target); publisher_signature_verified=$true; sha256=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant(); windows_11_ui_tested=$false} | ConvertTo-Json | Set-Content -LiteralPath ($target + '.signed-status.json') -Encoding UTF8
Write-Output 'Publisher signature verified. Windows 11 UI and delivery checks remain separate.'
