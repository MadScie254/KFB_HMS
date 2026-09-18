param([string]$ApplicationUrl = "http://127.0.0.1:8000")
$ErrorActionPreference = "Stop"
if (-not [Uri]::IsWellFormedUriString($ApplicationUrl, [UriKind]::Absolute)) { throw "ApplicationUrl must be an absolute URL." }
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Kingdom Faith Based Hospital.url"
$contents = @(
    "[InternetShortcut]",
    "URL=$ApplicationUrl",
    "IconFile=%SystemRoot%\System32\SHELL32.dll",
    "IconIndex=14"
) -join "`r`n"
[IO.File]::WriteAllText($shortcutPath, $contents, [Text.Encoding]::ASCII)
Write-Host "Shortcut created: $shortcutPath"

