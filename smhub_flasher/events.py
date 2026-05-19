# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import json
from typing import Any

JSON_FD_OBJ = None


def init_json_pipe(fd: int) -> None:
    """Initialize the JSON side-channel using the provided file descriptor."""
    global JSON_FD_OBJ
    if fd is not None and fd >= 0:
        import os
        import sys

        if sys.platform == "win32":
            import msvcrt

            try:
                fd = msvcrt.open_osfhandle(fd, os.O_WRONLY)
            except OSError as e:
                import logging

                logging.getLogger(__name__).warning(
                    f"Failed to open OS handle {fd}: {e}"
                )
                return
        try:
            JSON_FD_OBJ = os.fdopen(fd, "w", buffering=1, encoding="utf-8")
        except OSError as e:
            import logging

            logging.getLogger(__name__).warning(
                f"Failed to open JSON pipe FD {fd}: {e}"
            )


def emit(**kwargs: object) -> None:
    """Emit a structured JSON event to the side-channel pipe, if active."""
    if JSON_FD_OBJ is not None:
        try:
            payload = json.dumps(kwargs)
            JSON_FD_OBJ.write(payload + "\n")
            JSON_FD_OBJ.flush()
        except OSError:
            # If the pipe is broken (GUI closed unexpectedly), ignore
            pass


def emit_tqdm_progress(pbar: Any, label: str) -> None:
    """Helper to emit a progress event calculated from a tqdm instance."""
    if JSON_FD_OBJ is not None:
        try:
            d = pbar.format_dict
            rate = d.get("rate") or 0
            total = d.get("total") or 0
            n = d.get("n") or 0

            rem = (total - n) / rate if rate and total else 0
            pct = int(100 * n / total) if total else 0

            from tqdm.std import tqdm as std_tqdm

            rem_str = std_tqdm.format_interval(rem) if rem else "?"

            def format_bytes(n_bytes: float) -> str:
                if n_bytes < 1024 * 1024:
                    return f"{n_bytes / 1024:.0f}K"
                return f"{n_bytes / 1024 / 1024:.0f}M"

            emit(
                type="progress",
                percent=pct,
                current=format_bytes(n),
                total=format_bytes(total),
                label=label,
                remaining=rem_str,
            )
        except Exception:
            pass
