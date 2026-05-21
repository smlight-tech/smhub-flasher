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
uv sync --python 3.13 --extra build

ICON_PNG="../png/icon.png"
ICON_PNG_2X="../png/icon@2x.png"
ICON_SRC="$ICON_PNG_2X"
ICONSET_DIR="build/icon.iconset"
ICON_ICNS="build/SMHUB-Flasher.icns"

if [ ! -f "$ICON_SRC" ]; then
  ICON_SRC="$ICON_PNG"
fi

if [ -f "$ICON_SRC" ]; then
  echo ">> Building macOS app icon (.icns)"
  rm -rf "$ICONSET_DIR" "$ICON_ICNS"
  mkdir -p "$ICONSET_DIR"

  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    size2x=$((size * 2))
    sips -z "$size2x" "$size2x" "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
  done

  iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
else
  echo ">> Warning: icon source not found at $ICON_PNG or $ICON_PNG_2X"
fi

FASTBOOT="vendor/fastboot"
if [ ! -f "$FASTBOOT" ]; then
  echo ">> Downloading Google platform-tools (for fastboot)"
  mkdir -p vendor
  TMP_ZIP="vendor/platform-tools.zip"
  TMP_DIR="vendor/_pt"
  url="https://dl.google.com/android/repository/platform-tools_r35.0.1-darwin.zip"

  curl -L "$url" -o "$TMP_ZIP"
  
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
  --icon "$ICON_ICNS" \
  --add-data "web:web" \
  --add-data "vendor/fastboot:smhub_flasher" \
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
