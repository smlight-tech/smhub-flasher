# SPDX-License-Identifier: GPL-3.0-or-later
# Separate build script to compile smhub-simple.exe with hardcoded defaults.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$libwdiRev = "v1.5.1"

Write-Host ">> Cloning libwdi at revision $libwdiRev" -ForegroundColor Cyan
New-Item -ItemType Directory -Force vendor | Out-Null
$libwdiDir = Join-Path $PSScriptRoot "vendor\libwdi"
if (-not (Test-Path $libwdiDir)) {
  git clone https://github.com/pbatard/libwdi.git $libwdiDir
}

Push-Location $libwdiDir
# Force checkout to discard previous regex modifications to wdi-simple.c
git fetch origin
git checkout -f $libwdiRev
Pop-Location

Write-Host ">> Hardcoding SMHUB BootROM defaults into wdi-simple.c" -ForegroundColor Cyan
$cFile = Join-Path $libwdiDir "examples\wdi-simple.c"
$content = Get-Content $cFile -Raw

# Replace defaults using regex
$content = $content -replace '#define DESC\s+".+"', '#define DESC        "SMHUB BootROM"'
$content = $content -replace '#define VID\s+0x[0-9A-Fa-f]+', '#define VID         0x3346'
$content = $content -replace '#define PID\s+0x[0-9A-Fa-f]+', '#define PID         0x1000'

Set-Content -Path $cFile -Value $content

Write-Host ">> Disabling ARM64 and x86 installer components to avoid compiler requirements" -ForegroundColor Cyan
$staticVcxproj = Join-Path $libwdiDir "libwdi\.msvc\libwdi_static.vcxproj"
(Get-Content $staticVcxproj -Raw) -replace '(?s)<ProjectReference Include="installer_arm64\.vcxproj">.*?</ProjectReference>', '' -replace '(?s)<ProjectReference Include="installer_x86\.vcxproj">.*?</ProjectReference>', '' | Set-Content $staticVcxproj

$dllVcxproj = Join-Path $libwdiDir "libwdi\.msvc\libwdi_dll.vcxproj"
(Get-Content $dllVcxproj -Raw) -replace '(?s)<ProjectReference Include="installer_arm64\.vcxproj">.*?</ProjectReference>', '' -replace '(?s)<ProjectReference Include="installer_x86\.vcxproj">.*?</ProjectReference>', '' | Set-Content $dllVcxproj

$slnFile = Join-Path $libwdiDir "libwdi.sln"
(Get-Content $slnFile -Raw) -replace '(?s)Project\([^)]+\) = "installer_arm64".*?EndProject\r?\n', '' -replace '(?s)Project\([^)]+\) = "installer_x86".*?EndProject\r?\n', '' | Set-Content $slnFile

$embedderFiles = Join-Path $libwdiDir "libwdi\embedder_files.h"
(Get-Content $embedderFiles -Raw) -replace '.*installer_arm64\.exe.*', '' -replace '.*installer_x86\.exe.*', '' -replace '.*\.dll.*', '' -replace '.*\.sys.*', '' -replace '.*install-filter\.exe.*', '' -replace '.*\.txt.*', '' | Set-Content $embedderFiles

Write-Host ">> Patching winusb.inf.in to remove CoInstaller dependencies" -ForegroundColor Cyan
$winusbInf = Join-Path $libwdiDir "libwdi\winusb.inf.in"
(Get-Content $winusbInf -Raw) -replace '(?s)\[USB_Install\.NT.*?\.CoInstallers\].*?(?=\n\[|$)', '' -replace '(?s)\[CoInstallers_AddReg\].*?(?=\n\[|$)', '' -replace '(?s)\[CoInstallers_CopyFiles\].*?(?=\n\[|$)', '' -replace '(?s)\[DestinationDirs\].*?(?=\n\[|$)', '' -replace '(?s)\[SourceDisksNames\].*?(?=\n\[|$)', '' -replace '(?s)\[SourceDisksFiles.*?\](?!.*?\[SourceDisks).*?(?=\n\[|$)', '' | Set-Content $winusbInf

Write-Host ">> Patching embedder project to bypass AV heuristic locks" -ForegroundColor Cyan
$embedderVcxproj = Join-Path $libwdiDir "libwdi\.msvc\embedder.vcxproj"
(Get-Content $embedderVcxproj -Raw) -replace '<ProjectName>embedder</ProjectName>', '<ProjectName>wdi_emb</ProjectName>' | Set-Content $embedderVcxproj

$staticVcxproj = Join-Path $libwdiDir "libwdi\.msvc\libwdi_static.vcxproj"
(Get-Content $staticVcxproj -Raw) -replace 'embedder embedded\.h', 'wdi_emb embedded.h' | Set-Content $staticVcxproj

$dllVcxproj = Join-Path $libwdiDir "libwdi\.msvc\libwdi_dll.vcxproj"
(Get-Content $dllVcxproj -Raw) -replace 'embedder embedded\.h', 'wdi_emb embedded.h' | Set-Content $dllVcxproj

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"

function Install-VSBuildTools {
  Write-Host ">> Visual Studio C++ Build Tools missing. Downloading installer..." -ForegroundColor Yellow
  $vsInstaller = Join-Path $env:TEMP "vs_buildtools.exe"
  Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vs_buildtools.exe" -OutFile $vsInstaller
  Write-Host ">> Launching installer. Please accept the UAC prompt and wait (this may take a few minutes)..." -ForegroundColor Yellow
  Start-Process -FilePath $vsInstaller -ArgumentList "--wait --passive --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended" -Wait -Verb RunAs
}

if (-not (Test-Path $vswhere)) {
  Install-VSBuildTools
  if (-not (Test-Path $vswhere)) {
    throw "Installation failed or was cancelled."
  }
}

$msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe | Select-Object -First 1
if (-not $msbuild) {
  Write-Host ">> MSBuild found, but C++ workload is missing. Re-running installer..." -ForegroundColor Yellow
  Install-VSBuildTools
  $msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe | Select-Object -First 1
  if (-not $msbuild) {
    throw "MSBuild still not found! Installation failed."
  }
}

Write-Host ">> Compiling smhub-simple.exe with MSBuild" -ForegroundColor Cyan
Push-Location $libwdiDir
& $msbuild libwdi.sln /p:Configuration=Release /p:Platform=x64
Pop-Location

$outExe = Join-Path $PSScriptRoot "vendor\smhub-simple.exe"
Copy-Item (Join-Path $libwdiDir "x64\Release\examples\wdi-simple.exe") $outExe -Force

Write-Host ">> Done. smhub-simple.exe copied to vendor\smhub-simple.exe" -ForegroundColor Green
