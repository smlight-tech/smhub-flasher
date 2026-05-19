# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller build script for SMHUB Flasher GUI.
# Produces dist/SMHUB-Flasher.exe — single-file, no install.

param(
  [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Clean) {
  Remove-Item -Recurse -Force build, dist, .venv -ErrorAction SilentlyContinue
}

# 1. Virtual env
Write-Host ">> Syncing dependencies using uv" -ForegroundColor Cyan
uv sync --extra build

# 2. Verify driver installer (smhub-simple.exe)
$wdiSimple = Join-Path $PSScriptRoot "vendor\smhub-simple.exe"
if (-not (Test-Path $wdiSimple)) {
  Write-Host "!! smhub-simple.exe not found at vendor\smhub-simple.exe" -ForegroundColor Red
  Write-Host "!! Please run build-wdi.ps1 to generate it, or download the pre-compiled binary." -ForegroundColor Red
  exit 1
}

# 3. Verify / fetch Google fastboot.exe (and its required AdbWinApi.dll)
$fastboot = Join-Path $PSScriptRoot "vendor\fastboot.exe"
$adbDll   = Join-Path $PSScriptRoot "vendor\AdbWinApi.dll"
$adbUsbDll = Join-Path $PSScriptRoot "vendor\AdbWinUsbApi.dll"
if ((-not (Test-Path $fastboot)) -or (-not (Test-Path $adbDll))) {
  Write-Host ">> Downloading Google platform-tools (for fastboot.exe)" -ForegroundColor Cyan
  $tmpZip = Join-Path $PSScriptRoot "vendor\platform-tools.zip"
  $url    = "https://dl.google.com/android/repository/platform-tools_r35.0.1-win.zip"
  Invoke-WebRequest -Uri $url -OutFile $tmpZip
  $tmpDir = Join-Path $PSScriptRoot "vendor\_pt"
  Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
  Expand-Archive -Path $tmpZip -DestinationPath $tmpDir
  Copy-Item (Join-Path $tmpDir "platform-tools\fastboot.exe") $fastboot -Force
  Copy-Item (Join-Path $tmpDir "platform-tools\AdbWinApi.dll") $adbDll -Force
  Copy-Item (Join-Path $tmpDir "platform-tools\AdbWinUsbApi.dll") $adbUsbDll -Force
  Remove-Item -Recurse -Force $tmpDir
  Remove-Item $tmpZip
  Write-Host "   fastboot.exe size: $((Get-Item $fastboot).Length) bytes"
}

# 4. Clean up dangling processes
# Ensure any dangling processes from previous runs are killed to prevent file locks
Stop-Process -Name "SMHUB-Flasher" -Force -ErrorAction SilentlyContinue
Stop-Process -Name "fastboot" -Force -ErrorAction SilentlyContinue

# 5. Locate upstream smhub_flasher
$repoRoot = Split-Path -Parent $PSScriptRoot
$flasherPkg = Join-Path $repoRoot "smhub_flasher"
if (-not (Test-Path $flasherPkg)) {
  Write-Host "!! Upstream smhub_flasher/ package missing at $flasherPkg" -ForegroundColor Red
  exit 1
}

# 5. PyInstaller
Write-Host ">> Building executable" -ForegroundColor Cyan

# fastboot.exe + ADB DLLs need to live at the root of the bundle so the
# upstream smhub_flasher.main.resource_path("fastboot.exe") finds them.
uv run pyinstaller `
  --noconfirm `
  --clean `
  --name "SMHUB-Flasher" `
  --windowed `
  --onefile `
  --add-data "web;web" `
  --add-data "vendor\smhub-simple.exe;vendor" `
  --add-data "vendor\fastboot.exe;." `
  --add-data "vendor\AdbWinApi.dll;." `
  --add-data "vendor\AdbWinUsbApi.dll;." `
  --add-data "$flasherPkg;smhub_flasher" `
  --hidden-import "usb.backend.libusb1" `
  --hidden-import "fastcrc" `
  --hidden-import "packaging.version" `
  --hidden-import "tqdm" `
  --collect-all "webview" `
  --collect-all "libusb_package" `
  --collect-all "rich" `
  app.py

Write-Host ""
Write-Host ">> Done. Output: dist\SMHUB-Flasher.exe" -ForegroundColor Green
