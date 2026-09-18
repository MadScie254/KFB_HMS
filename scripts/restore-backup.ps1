param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$TargetDirectory,
    [string]$AgeIdentity = ""
)
$ErrorActionPreference = "Stop"
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$targetPath = [IO.Path]::GetFullPath($TargetDirectory)
if (Test-Path -LiteralPath $targetPath) {
    if ((Get-ChildItem -LiteralPath $targetPath -Force | Measure-Object).Count -gt 0) {
        throw "TargetDirectory must be empty. Restore only into an isolated test/recovery directory."
    }
} else {
    New-Item -ItemType Directory -Path $targetPath | Out-Null
}
$checksumPath = "$archivePath.sha256"
if (-not (Test-Path -LiteralPath $checksumPath)) { throw "Checksum sidecar not found: $checksumPath" }
$expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToUpperInvariant()
if ($expected -ne $actual) { throw "Backup checksum does not match. Do not restore this archive." }

$zipPath = $archivePath
if ($archivePath.EndsWith(".age", [StringComparison]::OrdinalIgnoreCase)) {
    if (-not $AgeIdentity) { throw "AgeIdentity is required for an encrypted archive." }
    $age = Get-Command age -ErrorAction Stop
    $zipPath = Join-Path $targetPath "decrypted-backup.zip"
    & $age.Source --decrypt --identity $AgeIdentity --output $zipPath $archivePath
    if ($LASTEXITCODE -ne 0) { throw "Backup decryption failed." }
}
[IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $targetPath)
Write-Host "Backup extracted and checksum verified in $targetPath"
Write-Host "Restore the database only on an isolated test/recovery instance, then follow docs/BACKUP_AND_RECOVERY.md."

