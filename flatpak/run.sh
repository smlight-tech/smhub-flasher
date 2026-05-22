#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
#
# Launcher for SMHUB Flasher GUI.
# Disables WebKit DMABuf renderer on Nvidia proprietary drivers,

if ls /dev/nvidia* >/dev/null 2>&1 || grep -qi nvidia /sys/bus/pci/devices/*/uevent 2>/dev/null; then
    export WEBKIT_DISABLE_DMABUF_RENDERER=1
fi

exec python3 /app/share/smhub-flasher-gui/app.py
