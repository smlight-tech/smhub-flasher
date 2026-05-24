# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
"""SMHUB Flasher GUI — pywebview entry point."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path


# Crash log — writes to a file next to the .exe so we can diagnose silent
# startup failures even when --windowed hides stdout/stderr.
def _crash_log_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), "smhub-flasher-crash.log"
        )
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "smhub-flasher-crash.log"
    )


def _log_crash(msg: str) -> None:
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            from datetime import datetime

            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def _excepthook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _log_crash(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook
_log_crash(
    f"=== startup === argv={sys.argv} frozen={getattr(sys, 'frozen', False)} executable={sys.executable}"
)

# PyInstaller on macOS may launch helper subprocesses via sys.executable with
# Python-style arguments (for example multiprocessing.resource_tracker).
if sys.platform == "darwin" and getattr(sys, "frozen", False) and "-c" in sys.argv:
    try:
        c_idx = sys.argv.index("-c")
        code = sys.argv[c_idx + 1] if c_idx + 1 < len(sys.argv) else ""
    except Exception:
        code = ""

    if code:
        exec(code, {"__name__": "__main__"})
        sys.exit(0)

# Re-entrant dispatch: when the frozen .exe is invoked with --run-flasher,
# delegate to the bundled smhub_flasher CLI instead of launching the GUI.
# This lets flasher_runner.py spawn the CLI as a subprocess via sys.executable.
if "--run-flasher" in sys.argv:
    sys.argv.remove("--run-flasher")
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass and meipass not in sys.path:
            sys.path.insert(0, meipass)

    # Force stdout/stderr to UTF-8 so Unicode box-drawing chars in the FSM
    # output survive the pipe to the GUI. PyInstaller's --windowed bootloader
    # otherwise defaults to cp1252 on Windows.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    import asyncio

    if sys.platform != "linux":
        try:
            import libusb_package  # bundles libusb-1.0.dll cross-platform
            import usb.backend.libusb1 as _libusb1

            _bundled_backend = _libusb1.get_backend(
                find_library=libusb_package.find_library
            )

            import usb.core as _usb_core

            _orig_find = _usb_core.find

            def _find_with_backend(*args, **kwargs):  # type: ignore[no-untyped-def]
                kwargs.setdefault("backend", _bundled_backend)
                return _orig_find(*args, **kwargs)

            _usb_core.find = _find_with_backend  # type: ignore[assignment]
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"warning: libusb_package setup failed: {e}\n")

    from smhub_flasher.main import async_main as flasher_main

    asyncio.run(flasher_main())
    sys.exit(0)

import webview
from driver_check import (
    TARGET_PIDS,
    TARGET_VIDS,
    usb_driver_check,
    launch_driver_installer,
)
from flasher_runner import FlasherRunner

import smhub_flasher.downloader as downloader
from console import SerialConsole


def resource_path(relative: str) -> str:
    """Resolve paths for both dev runs and PyInstaller onefile bundles."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def exe_dir() -> str:
    """Directory where the .exe lives (not _MEIPASS — that's the temp unpack dir)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_rom_path() -> str:
    """Default local firmware folder: <exe-dir>/rom if it exists, else empty."""
    candidate = os.path.join(exe_dir(), "rom")
    return candidate if os.path.isdir(candidate) else ""


def cache_dir() -> str:
    """Firmware zip cache. Lives next to .exe on Windows for portability, user cache on POSIX."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/smhub-flasher/firmware")
    elif sys.platform == "linux":
        return os.path.expanduser("~/.cache/smhub-flasher/firmware")
    return os.path.join(exe_dir(), "cache", "firmware")


def detect_sparse_image(path: str) -> bool:
    """True if the file starts with the Android Sparse magic 0xed26ff3a."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x3a\xff\x26\xed"
    except OSError:
        return False


class Api:
    """JS-callable bridge. Each method becomes window.pywebview.api.<method>()."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._runner: FlasherRunner | None = None
        self._download_thread: threading.Thread | None = None
        self._downloader: downloader.FirmwareDownloader | None = None
        self._console: SerialConsole | None = None

    def bind_window(self, window: webview.Window) -> None:
        self._window = window

    def set_log_expanded(self, expanded: bool) -> None:
        if not self._window:
            return
        self._window.resize(760, 900 if expanded else 620)

    def resize_window(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(width, height)

    def pick_folder(self) -> str | None:
        if not self._window:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else str(result)

    def validate_folder(self, folder: str) -> dict:
        p = Path(folder)
        fip = p / "fip.bin"
        emmc = p / "emmc.img"
        return {
            "fip_exists": fip.exists(),
            "emmc_exists": emmc.exists(),
            "fip_path": str(fip),
            "emmc_path": str(emmc),
        }

    # ---- Driver flow -------------------------------------------------------

    def check_driver(self) -> dict:
        if sys.platform == "win32":
            bound = usb_driver_check(TARGET_VIDS, TARGET_PIDS)
            return {"winusb_bound": bound, "platform": sys.platform}
        return {"platform": sys.platform}

    def install_driver(self) -> dict:
        try:
            rc = launch_driver_installer(resource_path("vendor/smhub-simple.exe"))
            return {"ok": rc == 0, "returncode": rc}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Default paths -----------------------------------------------------

    def get_default_rom_path(self) -> dict:
        path = default_rom_path()
        has_files = False
        if path:
            p = Path(path)
            has_files = (p / "fip.bin").exists() and (p / "emmc.img").exists()
        return {"path": path, "has_files": has_files}

    # ---- Online firmware ---------------------------------------------------

    def fetch_manifest(self, url: str | None = None) -> dict:
        try:
            target = url or downloader.MANIFEST_URL_DEFAULT
            dl = downloader.FirmwareDownloader(manifest_url=target)
            manifest = dl.fetch_manifest()
            return {"ok": True, "manifest": manifest}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def fetch_notes(self, url: str) -> dict:
        import urllib.request

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "SMHUB-Flasher/0.1"}
            )
            with urllib.request.urlopen(
                req, timeout=10.0, context=downloader._ssl_ctx()
            ) as resp:
                data = resp.read()
            return {"ok": True, "text": data.decode("utf-8")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def download_and_flash(
        self,
        manifest_url: str,
        channel: str,
        version: str,
        force_redownload: bool = False,
    ) -> dict:
        if self._runner and self._runner.is_running():
            return {"ok": False, "error": "already running"}
        if self._download_thread and self._download_thread.is_alive():
            return {"ok": False, "error": "download already running"}

        target_url = manifest_url or downloader.MANIFEST_URL_DEFAULT
        cache = cache_dir()

        dl = downloader.FirmwareDownloader(manifest_url=target_url, cache_dir=cache)
        self._downloader = dl

        def emit(event: dict) -> None:
            if self._window:
                self._window.evaluate_js(
                    f"window.onFlasherEvent && window.onFlasherEvent({_js_safe(event)})"
                )

        # Override CLI tracking and use GUI emit callback
        dl._progress_cb = emit  # type: ignore

        def work() -> None:
            try:
                # If version is 'latest', we pass the channel as the argument to match execution signatures
                version_arg = channel if version == "latest" else version

                rom_dir, temp_dir, is_fastboot = dl.execute(
                    version_arg, force_redownload=force_redownload
                )

                # Emit the eMMC size (for ETA) — read the extracted file.
                try:
                    emmc_path = os.path.join(rom_dir, "emmc.img")
                    if os.path.exists(emmc_path):
                        emit(
                            {
                                "type": "image_size",
                                "bytes": os.path.getsize(emmc_path),
                            }
                        )
                except Exception:
                    pass
                emit({"type": "prep_phase", "phase": "Ready to flash"})
                self._runner = FlasherRunner(
                    image_dir=rom_dir, on_event=emit, is_fastboot=is_fastboot
                )
                self._runner.run()
            except downloader.CancelledError:
                emit({"type": "log", "line": "Download cancelled by user."})
                emit({"type": "status", "status": "cancelled", "returncode": -1})
            except Exception as e:
                emit({"type": "error", "message": str(e)})
                emit({"type": "status", "status": "error", "returncode": -1})
            finally:
                if self._downloader:
                    self._downloader.cleanup()

        self._download_thread = threading.Thread(target=work, daemon=True)
        self._download_thread.start()
        return {"ok": True}

    def get_cache_info(self) -> dict:
        entries = downloader.FirmwareDownloader.list_cache(cache_dir())
        total = sum(e["size"] for e in entries)
        return {"count": len(entries), "total_bytes": total, "path": cache_dir()}

    def clear_firmware_cache(self) -> dict:
        removed = downloader.FirmwareDownloader.clear_cache(cache_dir())
        return {"ok": True, "removed": removed}

    # ---- Flashing ----------------------------------------------------------

    def start_flash(self, folder: str) -> dict:
        if self._runner and self._runner.is_running():
            return {"ok": False, "error": "already running"}

        def emit(event: dict) -> None:
            if self._window:
                self._window.evaluate_js(
                    f"window.onFlasherEvent && window.onFlasherEvent({_js_safe(event)})"
                )

        # Emit the eMMC image size so the GUI can compute an initial ETA, and
        # detect whether the image is Android Sparse → fastboot mode.
        is_fastboot = False
        try:
            emmc_path = Path(folder) / "emmc.img"
            if emmc_path.exists():
                emit(
                    {
                        "type": "image_size",
                        "bytes": emmc_path.stat().st_size,
                    }
                )
                is_fastboot = detect_sparse_image(str(emmc_path))
                if is_fastboot:
                    emit(
                        {
                            "type": "log",
                            "line": "Detected Android Sparse image — using fastboot mode.",
                        }
                    )
        except Exception:
            pass

        self._runner = FlasherRunner(
            image_dir=folder, on_event=emit, is_fastboot=is_fastboot
        )
        t = threading.Thread(target=self._runner.run, daemon=True)
        t.start()
        return {"ok": True}

    def cancel_flash(self) -> dict:
        cancelled_any = False
        # Set the download-cancel flag in case we're still in download/extract.
        if self._downloader:
            self._downloader.cancel()

        if self._download_thread and self._download_thread.is_alive():
            cancelled_any = True

        # Also stop the flasher subprocess if it's running.
        if self._runner and self._runner.is_running():
            self._runner.cancel()
            cancelled_any = True

        return {"ok": cancelled_any}

    # ---- Console -----------------------------------------------------------

    def open_console(self) -> dict:
        if self._console:
            self._console.disconnect()

        def on_data(text: str) -> None:
            if self._window:
                safe_str = _js_safe(text)
                self._window.evaluate_js(f"window.writeTerminalData({safe_str})")

        def on_disconnect(reason: str = "Connection lost") -> None:
            if self._window:
                self._window.evaluate_js(
                    f"window.setConsoleStatus('Disconnected: {reason}', '#f00')"
                )
                # Auto-change button state back to Connect
                self._window.evaluate_js(
                    "document.getElementById('btn-console-connect').textContent = 'Connect';"
                )

        try:
            self._console = SerialConsole(on_data, on_disconnect)
            self._console.connect()
            return {"ok": True}
        except Exception as e:
            self._console = None
            return {"ok": False, "error": str(e)}

    def close_console(self) -> dict:
        if self._console:
            self._console.disconnect()
            self._console = None
        return {"ok": True}

    def write_console(self, data: str) -> dict:
        if self._console:
            self._console.write(data)
        return {"ok": True}

    def console_push_file(self) -> dict:
        if not self._console:
            return {"ok": False, "error": "Console disconnected"}

        result = self._window.create_file_dialog(webview.FileDialog.OPEN)
        if not result:
            return {"ok": False, "error": "No file selected"}
        file_path = result[0] if isinstance(result, (list, tuple)) else str(result)

        import shutil
        import time

        sz_bin = resource_path(os.path.join("vendor", f"sz{'.exe' if sys.platform == 'win32' else ''}"))
        if not os.path.exists(sz_bin):
            sz_bin = shutil.which("sz")
            if not sz_bin:
                return {
                    "ok": False,
                    "error": "ZMODEM 'sz' binary not found on host system. Install lrzsz.",
                }

        self._console.write("rz\r")
        time.sleep(0.5)

        success = self._console.run_zmodem([sz_bin, "-b", "-e", file_path])
        return {"ok": success}

    def console_pull_logs(self) -> dict:
        if not self._console:
            return {"ok": False, "error": "Console disconnected"}

        folder = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not folder:
            return {"ok": False, "error": "No destination selected"}
        folder = folder[0] if isinstance(folder, (list, tuple)) else str(folder)

        import shutil
        import time

        rz_bin = resource_path(os.path.join("vendor", f"rz{'.exe' if sys.platform == 'win32' else ''}"))
        if not os.path.exists(rz_bin):
            rz_bin = shutil.which("rz")
            if not rz_bin:
                return {
                    "ok": False,
                    "error": "ZMODEM 'rz' binary not found on host system. Install lrzsz.",
                }

        # NOTE: Cannot use sudo here because it requires a password on the device and would hang the transfer.
        self._console.write(
            "tar -czf /tmp/smhub_logs.tar.gz -C /var/log . && sz /tmp/smhub_logs.tar.gz\r"
        )
        time.sleep(1.0)
        success = self._console.run_zmodem([rz_bin, "-b", "-e"], cwd=folder)
        return {"ok": success}

    def console_pull_backup(self) -> dict:
        if not self._console:
            return {"ok": False, "error": "Console disconnected"}

        folder = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not folder:
            return {"ok": False, "error": "No destination selected"}
        folder = folder[0] if isinstance(folder, (list, tuple)) else str(folder)

        import shutil
        import time

        rz_bin = resource_path(os.path.join("vendor", f"rz{'.exe' if sys.platform == 'win32' else ''}"))
        if not os.path.exists(rz_bin):
            rz_bin = shutil.which("rz")
            if not rz_bin:
                return {
                    "ok": False,
                    "error": "ZMODEM 'rz' binary not found on host system. Install lrzsz.",
                }

        # Use the newly added --backup-only flag
        self._console.write(
            "factory-reset --backup-only /tmp/smhub_backup.tar.xz && sz /tmp/smhub_backup.tar.xz\r"
        )
        time.sleep(
            2.0
        )  # Give the script some time to generate the backup before catching
        success = self._console.run_zmodem([rz_bin, "-b", "-e"], cwd=folder)
        return {"ok": success}


def _js_safe(obj: dict) -> str:
    import json

    return json.dumps(obj)


def _hook_dpi_nudge(window: webview.Window) -> None:
    """Nudge window size on DPI change to force a WebKit repaint on Linux/NVIDIA."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib  # type: ignore[import-untyped]
        from webview.platforms.gtk import BrowserView  # type: ignore[import-untyped]

        def _connect() -> bool:
            bv = BrowserView.instances.get(window.uid)
            if not bv:
                return False
            gtk_win = bv.window

            def _on_scale_change(*_: object) -> None:
                def _nudge() -> bool:
                    w, h = gtk_win.get_size()
                    gtk_win.resize(w + 1, h)
                    GLib.idle_add(gtk_win.resize, w, h)
                    return False

                for delay in (50, 200, 500, 1000):
                    GLib.timeout_add(delay, _nudge)

            gtk_win.connect("notify::scale-factor", _on_scale_change)
            return False

        GLib.idle_add(_connect)
    except Exception:
        pass


def main() -> None:
    if sys.platform == "linux":
        os.environ.setdefault("GIO_USE_VFS", "local")

    api = Api()
    window = webview.create_window(
        title="SMHUB Flasher",
        url=resource_path("web/index.html"),
        js_api=api,
        width=760,
        height=620,
        min_size=(680, 560),
        resizable=True,
        background_color="#1a1d24",
        transparent=False,
    )
    api.bind_window(window)
    if sys.platform == "linux" and os.environ.get("WEBKIT_DISABLE_DMABUF_RENDERER"):
        window.events.loaded += lambda: _hook_dpi_nudge(window)
    try:
        webview.start(debug=False)
    finally:
        # Best-effort cleanup so PyInstaller's bootloader can delete _MEI*.
        try:
            if api._runner and api._runner.is_running():
                api._runner.cancel()
                # Give the subprocess a moment to release its pipe handles.
                if api._runner._proc is not None:
                    try:
                        api._runner._proc.wait(timeout=2.0)
                    except Exception:
                        try:
                            api._runner._proc.kill()
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            if api._downloader:
                api._downloader.cancel()
                api._downloader.cleanup()
        except Exception:
            pass
        try:
            if api._console:
                api._console.disconnect()
        except Exception:
            pass

        import time

        # Workaround for PyInstaller + PyWebView on Windows:
        # Give the Edge Chromium (WebView2) subprocesses a moment to fully terminate
        # and release locks on WebView2Loader.dll inside the _MEI temporary directory.
        # Otherwise, the PyInstaller bootloader throws an "Access Denied" warning.
        time.sleep(2.0)


if __name__ == "__main__":
    # Clean up any dangling fastboot processes from previous aborted flashes
    # to ensure the USB interface isn't locked before we even start.
    try:
        import subprocess

        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "fastboot.exe"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
