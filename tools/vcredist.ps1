# Microsoft Visual C++ 2015-2022 runtime (msvcp140.dll) check / update / download.
# Libraries built with Visual Studio 2022 17.10+ (onnxruntime, ctranslate2 ...) crash with an
# access violation on start when the system msvcp140.dll is older than 14.40 (constexpr std::mutex).
#   -Check            print "OK <ver>", "OLD <ver>" or "MISSING"; exit 0 when OK, else 1
#   -Install          update the runtime (winget, then the official installer); exit 0 when OK afterwards
#   -Download <file>  save the official vc_redist.x64.exe to <file> (bundled into the setup package)
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
param([switch]$Check, [switch]$Install, [string]$Download = "")

$ErrorActionPreference = "SilentlyContinue"
$Need = [version]"14.40.0.0"
$Url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

function Get-RuntimeVersion {
    $f = Join-Path $env:WINDIR "System32\msvcp140.dll"
    if (Test-Path $f) {
        $vi = (Get-Item $f).VersionInfo
        return New-Object System.Version $vi.FileMajorPart, $vi.FileMinorPart, $vi.FileBuildPart, $vi.FilePrivatePart
    }
    return $null
}

function Get-State {
    $v = Get-RuntimeVersion
    if (-not $v) { return "MISSING" }
    if ($v -ge $Need) { return "OK $v" }
    return "OLD $v"
}

function Save-Installer([string]$Path) {
    $dir = Split-Path -Parent $Path
    if ($dir) { New-Item -ItemType Directory -Force $dir | Out-Null }
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing $Url -OutFile $Path
    } catch { }
    return ((Test-Path $Path) -and ((Get-Item $Path).Length -gt 10MB))
}

if ($Download) {
    if (Save-Installer $Download) { Write-Output "saved $Download"; exit 0 }
    Remove-Item $Download -Force
    Write-Output "download failed"
    exit 1
}

if ($Install) {
    if (Get-Command winget) {
        winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements --silent | Out-Host
        if ((Get-State) -like "OK*") { Write-Output (Get-State); exit 0 }
    }
    $exe = Join-Path $env:TEMP "vc_redist.x64.exe"
    if (Save-Installer $exe) {
        # needs administrator rights: Windows shows a UAC prompt
        $p = Start-Process -FilePath $exe -ArgumentList "/install", "/passive", "/norestart" -Verb RunAs -Wait -PassThru
        if ($p) { Write-Output "vc_redist exit code $($p.ExitCode)" }
    } else {
        Write-Output "download failed: $Url"
    }
    $s = Get-State
    Write-Output $s
    if ($s -like "OK*") { exit 0 }
    exit 1
}

$s = Get-State
Write-Output $s
if ($s -like "OK*") { exit 0 }
exit 1
