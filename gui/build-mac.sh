#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
# PyInstaller build script for SMHUB Flasher MacOS GUI.
# Produces dist/SMHUB-Flasher.app bundle.

set -e

cd "$(dirname "$0")"

if [ "$1" = "--clean" ]; then
  rm -rf build dist .venv
fi

echo ">> Syncing dependencies using uv"
uv sync --extra build

FASTBOOT="vendor/fastboot"
if [ ! -f "$FASTBOOT" ]; then
  echo ">> Downloading Google platform-tools (for fastboot)"
  TMP_ZIP="vendor/platform-tools.zip"
  TMP_DIR="vendor/_pt"
  
  curl -L "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip" -o "$TMP_ZIP"
  
  rm -rf "$TMP_DIR"
  unzip -q "$TMP_ZIP" -d "$TMP_DIR"
  cp "$TMP_DIR/platform-tools/fastboot" "$FASTBOOT"
  
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
  --add-data "web:web" \
  --add-data "vendor/fastboot:." \
  --add-data "$FLASHER_PKG:smhub_flasher" \
  --hidden-import "usb.backend.libusb1" \
  --hidden-import "fastcrc" \
  --hidden-import "packaging.version" \
  --hidden-import "tqdm" \
  --collect-all "webview" \
  --collect-all "libusb_package" \
  --collect-all "rich" \
  --collect-all "certifi" \
  app.py

echo ""
echo ">> Done. Output: dist/SMHUB-Flasher.app"
