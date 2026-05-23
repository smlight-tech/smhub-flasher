#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

if ls /dev/nvidia0 >/dev/null 2>&1; then
    # NVIDIA: DMABuf renderer causes a hard runtime failure without this flag.
    export WEBKIT_DISABLE_DMABUF_RENDERER=1
fi

exec python3 /app/share/smhub-flasher-gui/app.py
