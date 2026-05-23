# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT


class UsbPermissionError(PermissionError):
    """Raised when the USB device cannot be opened due to insufficient permissions (EACCES)."""
