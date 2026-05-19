# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
"""Spawns `python -m smhub_flasher` and parses its stdout into GUI events.

The original CLI is not modified; we consume its human-readable output and
translate it to structured events for the webview frontend.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

EventSink = Callable[[dict], None]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


PHASES = [
    "BootROM Detection",
    "BootROM Handshake",
    "U-Boot Load",  # synthetic — emitted when we see "U-Boot FIP loaded"
    "eMMC Flash",
    "Done",
]


class FlasherRunner:
    def __init__(
        self, image_dir: str, on_event: EventSink, is_fastboot: bool = False
    ) -> None:
        self.image_dir = image_dir
        self.on_event = on_event
        self.is_fastboot = is_fastboot
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._stage2_total: int = 0
        self._stage2_sent: int = 0
        self._in_stage2: bool = False
        # The FSM doesn't emit a "▶ U-Boot Load" section header, so we
        # synthesize one the first time we see U-Boot FIP progress.
        self._current_phase: str | None = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                if sys.platform == "win32":
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self._proc.terminate()
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

            # Since terminate() on Windows only kills the parent Python process,
            # any fastboot process it spawned is orphaned and holds USB locks.
            if sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", "fastboot.exe"],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass

    def _emit(self, **kwargs: object) -> None:
        try:
            self.on_event(dict(kwargs))
        except Exception:
            pass

    def _project_root(self) -> Path:
        # When frozen by PyInstaller, the original package is bundled alongside.
        # In dev, walk up from gui/ to repo root.
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
            return base
        return Path(__file__).resolve().parent.parent

    def run(self) -> None:
        self._emit(type="status", status="starting")

        root = self._project_root()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["COLORAMA_DISABLE"] = "1"
        # Rich (used by upstream main.py) also colorizes; force plain text.
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, False)
        os.set_inheritable(w_fd, True)

        if sys.platform == "win32":
            import msvcrt

            pass_fd = msvcrt.get_osfhandle(w_fd)
        else:
            pass_fd = w_fd

        if getattr(sys, "frozen", False):
            cmd = [
                sys.executable,
                "--run-flasher",
                "--image-dir",
                self.image_dir,
                "--json-fd",
                str(pass_fd),
            ]
        else:
            cmd = [
                sys.executable,
                "-u",
                "-m",
                "smhub_flasher",
                "--image-dir",
                self.image_dir,
                "--json-fd",
                str(pass_fd),
            ]

        if self.is_fastboot:
            cmd.append("--fastboot")

        # Start standard log level (remove forced verbose)
        # cmd.extend(["--verbose", "--verbose"])

        kwargs = {
            "cwd": str(root),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 1,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }

        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP — needed for sending CTRL_BREAK_EVENT
            # CREATE_NO_WINDOW       — suppresses the brief console flash
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            kwargs["close_fds"] = False
        else:
            kwargs["pass_fds"] = (w_fd,)

        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError as e:
            os.close(r_fd)
            os.close(w_fd)
            self._emit(type="error", message=f"Failed to launch flasher: {e}")
            self._emit(type="status", status="error", returncode=-1)
            return

        os.close(w_fd)

        self._emit(type="status", status="running")

        def json_reader_thread() -> None:
            with os.fdopen(r_fd, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)

                        if (
                            data.get("type") == "progress"
                            and data.get("label") == "U-Boot FIP"
                        ):
                            if self._current_phase != "U-Boot Load":
                                self._current_phase = "U-Boot Load"
                                self._emit(type="phase", phase="U-Boot Load")

                        if data.get("type") == "phase":
                            phase = data.get("phase")
                            if phase in ("eMMC Flash (CVI)", "Fastboot Flash"):
                                phase = "eMMC Flash"
                            self._current_phase = phase
                            data["phase"] = phase

                        self._emit(**data)
                    except json.JSONDecodeError:
                        self._emit(type="log", line=f"[json parse error] {line}")

        t = threading.Thread(target=json_reader_thread, daemon=True)
        t.start()

        assert self._proc.stdout is not None

        def stdout_reader_thread() -> None:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
                clean_line = _strip_ansi(line).rstrip("\r\n")
                if clean_line.strip():
                    self._emit(type="log", line=clean_line)

        t2 = threading.Thread(target=stdout_reader_thread, daemon=True)
        t2.start()

        self._proc.wait()

        # Wait for JSON thread to finish processing its pipe
        t.join(timeout=2.0)

        if self._cancelled:
            self._emit(
                type="status", status="cancelled", returncode=self._proc.returncode
            )
        elif self._proc.returncode == 0:
            self._emit(type="phase", phase="Done")
            self._emit(
                type="status", status="success", returncode=self._proc.returncode
            )
        else:
            self._emit(type="status", status="error", returncode=self._proc.returncode)
