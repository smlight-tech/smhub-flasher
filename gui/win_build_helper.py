#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
"""Windows build helper: generates icon.ico and invokes PyInstaller."""

import argparse
import io
import struct
import subprocess
import sys
from pathlib import Path


def make_ico(src_png: Path, out_ico: Path) -> None:
    """Write a proper multi-size ICO."""
    from PIL import Image

    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img = Image.open(src_png).convert("RGBA")
    img.save(out_ico, format="ICO", sizes=sizes)
    print(f"  icon.ico: {out_ico.stat().st_size:,} bytes, {len(sizes)} sizes")


def run_pyinstaller(icon_ico: Path, icon_png: Path, flasher_pkg: Path) -> None:
    here = Path(__file__).parent
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", "SMHUB-Flasher",
        "--windowed",
        "--onefile",
        "--icon", str(icon_ico),
        "--add-data", f"web{_sep()}web",
        "--add-data", f"{icon_png.parent}{_sep()}png",
        "--add-data", f"vendor/smhub-simple.exe{_sep()}vendor",
        "--add-data", f"vendor/fastboot.exe{_sep()}smhub_flasher",
        "--add-data", f"vendor/AdbWinApi.dll{_sep()}smhub_flasher",
        "--add-data", f"vendor/AdbWinUsbApi.dll{_sep()}smhub_flasher",
        "--add-data", f"{flasher_pkg}{_sep()}smhub_flasher",
        "--hidden-import", "usb.backend.libusb1",
        "--hidden-import", "fastcrc",
        "--hidden-import", "packaging.version",
        "--hidden-import", "tqdm",
        "--collect-all", "webview",
        "--collect-all", "libusb_package",
        "--collect-all", "rich",
        "app.py",
    ]
    subprocess.run(cmd, cwd=here, check=True)


def _sep() -> str:
    """PyInstaller --add-data separator: ';' on Windows, ':' elsewhere."""
    import os
    return ";" if os.name == "nt" else ":"


def main() -> None:
    parser = argparse.ArgumentParser(description="Icon + PyInstaller build helper")
    parser.add_argument("--icon-png", required=True, type=Path, help="Source icon PNG")
    parser.add_argument("--flasher-pkg", required=True, type=Path, help="Path to smhub_flasher/ package")
    args = parser.parse_args()

    here = Path(__file__).parent
    icon_ico = here / "icon.ico"

    print(">> Generating icon.ico")
    make_ico(args.icon_png, icon_ico)

    print(">> Running PyInstaller")
    run_pyinstaller(icon_ico, args.icon_png, args.flasher_pkg)


if __name__ == "__main__":
    main()
