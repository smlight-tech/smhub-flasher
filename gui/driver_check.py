# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
"""Detect whether WinUSB is bound to the SMHUB device, and launch libwdi's
smhub-simple.exe (compiled from wdi-simple) to install it when not."""

from __future__ import annotations

import os
import sys
from typing import Iterable

TARGET_VIDS: list[int] = [0x3346]
TARGET_PIDS: list[int] = [0x1000]


def _iter_enum_usb_keys() -> Iterable[tuple[int, int, str]]:
    """Yield (vid, pid, service) for every attached USB device in the registry."""
    if sys.platform != "win32":
        return

    import winreg

    root = winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(root, r"SYSTEM\CurrentControlSet\Enum\USB") as usb_key:
            i = 0
            while True:
                try:
                    vid_pid = winreg.EnumKey(usb_key, i)
                except OSError:
                    break
                i += 1
                # format: VID_3346&PID_1000
                try:
                    vid_s, pid_s = vid_pid.split("&")
                    vid = int(vid_s.split("_")[1], 16)
                    pid = int(pid_s.split("_")[1], 16)
                except (ValueError, IndexError):
                    continue

                try:
                    with winreg.OpenKey(usb_key, vid_pid) as dev_key:
                        j = 0
                        while True:
                            try:
                                inst = winreg.EnumKey(dev_key, j)
                            except OSError:
                                break
                            j += 1
                            try:
                                with winreg.OpenKey(dev_key, inst) as ikey:
                                    service, _ = winreg.QueryValueEx(ikey, "Service")
                                    yield vid, pid, str(service)
                            except OSError:
                                continue
                except OSError:
                    continue
    except OSError:
        return


def usb_driver_check(vids: list[int], pids: list[int]) -> bool:
    """Return True if the USB device has the required driver/permissions (WinUSB on Windows)."""
    if sys.platform != "win32":
        return True
    for vid, pid, service in _iter_enum_usb_keys():
        if vid in vids and pid in pids and service.lower() == "winusb":
            return True
    return False


def launch_driver_installer(
    installer_path: str,
    vid: int = 0x3346,
    pid: int = 0x1000,
) -> int:
    """Invoke smhub-simple.exe under UAC to extract files, then trust cert and install via pnputil."""
    if not os.path.exists(installer_path):
        raise FileNotFoundError(
            f"Driver installer not found at {installer_path}. "
            "Please ensure smhub-simple.exe is built and bundled."
        )

    import tempfile
    ext_dir = os.path.join(tempfile.gettempdir(), "smhub_usb_driver")
    ps_script_path = os.path.join(tempfile.gettempdir(), "smhub_install.ps1")

    ps_content = f"""$ErrorActionPreference = 'Stop'
Start-Transcript -Path "$env:TEMP\\smhub_install.log"
& "{installer_path}" -d "{ext_dir}"
if (-not $?) {{ exit 1 }}

$catPath = "{ext_dir}\\usb_device.cat"
Start-Sleep -Seconds 1
if (Test-Path $catPath) {{
    $cert = (Get-AuthenticodeSignature $catPath).SignerCertificate
    if ($cert) {{
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store "TrustedPublisher", "LocalMachine"
        $store.Open("ReadWrite")
        $store.Add($cert)
        $store.Close()
    }}
}}

$Signature = @"
[DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool UpdateDriverForPlugAndPlayDevices(
    IntPtr hwndParent,
    string HardwareId,
    string FullInfPath,
    uint InstallFlags,
    out bool bRebootRequired);
"@
Add-Type -MemberDefinition $Signature -Name "DriverInstaller" -Namespace "Win32"

$reboot = $false
$hwid = "USB\\VID_3346&PID_1000"
$infPath = "{ext_dir}\\usb_device.inf"
$result = [Win32.DriverInstaller]::UpdateDriverForPlugAndPlayDevices([IntPtr]::Zero, $hwid, $infPath, 1, [ref]$reboot)

if (-not $result) {{
    exit [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
}}
"""
    with open(ps_script_path, "w") as f:
        f.write(ps_content)

    if sys.platform == "win32":
        import ctypes

        SEE_MASK_NOCLOSEPROCESS = 0x00000040

        class SHELLEXECUTEINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p),
                ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p),
                ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p),
                ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong),
                ("hIconOrMonitor", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p),
            ]

        sei = SHELLEXECUTEINFO()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = "powershell.exe"
        sei.lpParameters = f"-ExecutionPolicy Bypass -WindowStyle Hidden -File \"{ps_script_path}\""
        sei.nShow = 0  # SW_HIDE

        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not ok:
            raise RuntimeError("ShellExecuteExW failed (user may have cancelled UAC)")

        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
        rc = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(rc))
        kernel32.CloseHandle(sei.hProcess)
        return int(rc.value)

    return 0
