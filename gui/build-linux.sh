#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
# PyInstaller build script for SMHUB Flasher Linux GUI.
# Produces dist/SMHUB-Flasher executable.

set -e

cd "$(dirname "$0")"

if [ "$1" = "--clean" ]; then
  rm -rf build dist .venv vendor/fastboot vendor/_pt vendor/platform-tools.zip
fi

if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists cairo gobject-introspection-1.0; then
  echo "!! Missing system dependencies required to build PyGObject and pywebview for Linux."
  echo "   Please run the following command to install them:"
  echo "   sudo apt-get install -y build-essential libgirepository1.0-dev libgirepository-2.0-dev libglib2.0-dev libcairo2-dev pkg-config python3-dev gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-dev udev"
  exit 1
fi

echo ">> Syncing dependencies using uv"
uv sync --extra build

FASTBOOT="vendor/fastboot"
if [ ! -f "$FASTBOOT" ]; then
  echo ">> Downloading Google platform-tools (for fastboot)"
  TMP_ZIP="vendor/platform-tools.zip"
  TMP_DIR="vendor/_pt"
  
  mkdir -p vendor
  curl -L "https://dl.google.com/android/repository/platform-tools_r35.0.1-linux.zip" -o "$TMP_ZIP"
  
  rm -rf "$TMP_DIR"
  unzip -q "$TMP_ZIP" -d "$TMP_DIR"
  cp "$TMP_DIR/platform-tools/fastboot" "$FASTBOOT"
  chmod +x "$FASTBOOT"
  
  rm -rf "$TMP_DIR"
  rm -f "$TMP_ZIP"
  
  SIZE=$(wc -c < "$FASTBOOT" | tr -d ' ')
  echo "   fastboot size: ${SIZE} bytes"
fi

FLASHER_PKG="../smhub_flasher"
if [ ! -d "$FLASHER_PKG" ]; then
  echo "!! Upstream smhub_flasher/ package missing at $FLASHER_PKG"
  exit 1
fi

echo ">> Building executable"

uv run pyinstaller \
  --noconfirm \
  --clean \
  --name "SMHUB-Flasher" \
  --windowed \
  --onefile \
  --add-data "web:web" \
  --add-data "vendor/fastboot:smhub_flasher" \
  --add-data "$FLASHER_PKG:smhub_flasher" \
  --hidden-import "usb.backend.libusb1" \
  --hidden-import "fastcrc" \
  --hidden-import "packaging.version" \
  --hidden-import "pyudev" \
  --hidden-import "tqdm" \
  --collect-all "gi" \
  --collect-all "webview" \
  --collect-all "libusb_package" \
  --collect-all "rich" \
  --collect-all "certifi" \
  app.py

echo ""
echo ">> Done. Output: dist/SMHUB-Flasher"
