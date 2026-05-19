# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
"""Fetch firmware manifest, download zip, verify SHA-256, extract ROM files."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, cast

from packaging.version import Version

MANIFEST_URL_DEFAULT = (
    "https://updates.smlight.tech/firmware/smhub/utils/os-flasher.json"
)
REQUIRED_NAMES = ("fip.bin", "emmc.img")


class CancelledError(Exception):
    """Raised when the user requested cancellation mid-operation."""


def _ssl_ctx() -> ssl.SSLContext:
    os.environ.pop("SSLKEYLOGFILE", None)
    import sys

    if sys.platform == "darwin":
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return ssl.create_default_context()


class FirmwareDownloader:
    """Stateful firmware downloader managing cache, paths, and progress UX."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        manifest_url: str = MANIFEST_URL_DEFAULT,
        cache_dir: str | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.manifest_url = manifest_url
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "smhub-flasher"
        )
        self.manifest: dict[str, Any] | None = None
        self.temp_dir: str | None = None
        self._should_cancel = False

    def fetch_manifest(self) -> dict[str, Any]:
        """Fetch and cache the network manifest."""
        if self.manifest is not None:
            return self.manifest

        req = urllib.request.Request(
            self.manifest_url, headers={"User-Agent": "SMHUB-Flasher/0.1"}
        )
        with urllib.request.urlopen(req, timeout=15.0, context=_ssl_ctx()) as resp:
            data = resp.read()
        manifest = cast(dict[str, Any], json.loads(data.decode("utf-8")))
        if manifest.get("manifest_version") != 1:
            raise ValueError(
                f"Unsupported manifest_version: {manifest.get('manifest_version')!r}"
            )

        if "releases" in manifest:

            def sort_key(r: dict[str, Any]) -> Version:
                try:
                    return Version(r.get("version", "0.0.0"))
                except Exception:
                    return Version("0.0.0")

            manifest["releases"].sort(key=sort_key, reverse=True)

        self.manifest = manifest
        return self.manifest

    def _progress_cb(self, info: dict[str, Any]) -> None:
        """Internal callback to handle UX transitions. Overridden in subclasses."""
        pass

    def cancel(self) -> None:
        self._should_cancel = True

    def resolve_version(self, version_arg: str) -> tuple[str, str, dict[str, Any]]:
        """Resolve argument correctly to (channel, version, release)."""
        manifest = self.fetch_manifest()
        releases = manifest.get("releases", [])

        if version_arg == "latest":
            version_arg = "stable"

        known_channels = {r.get("channel", "stable") for r in releases} | {
            "stable",
            "dev",
            "beta",
        }
        if version_arg in known_channels:
            for r in releases:
                if r.get("channel", "stable") == version_arg:
                    return version_arg, str(r.get("version")), r
            raise ValueError(f"No releases found for channel '{version_arg}'")

        for r in releases:
            if r.get("version") == version_arg:
                return r.get("channel", "stable"), version_arg, r

        raise ValueError(f"Version '{version_arg}' not found in any channel")

    def download_file(
        self, url: str, dest_path: str, expected_sha256: str | None = None
    ) -> None:
        hasher = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "SMHUB-Flasher/0.1"})
        with urllib.request.urlopen(req, timeout=30.0, context=_ssl_ctx()) as resp:
            total_size = int(resp.headers.get("Content-Length", "0"))
            downloaded = 0
            with open(dest_path, "wb") as f:
                while True:
                    if self._should_cancel:
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        raise CancelledError("Download cancelled")
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)

                    pct = int(100 * downloaded / total_size) if total_size else 0
                    self._progress_cb(
                        {
                            "type": "download_progress",
                            "downloaded": downloaded,
                            "total": total_size,
                            "percent": pct,
                        }
                    )

        if expected_sha256:
            actual = hasher.hexdigest().lower()
            if actual != expected_sha256.lower():
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                raise ValueError(
                    f"SHA-256 mismatch: expected {expected_sha256}, got {actual}"
                )

    def extract_rom_files(self, zip_path: str, dest_dir: str) -> dict[str, str]:
        os.makedirs(dest_dir, exist_ok=True)
        extracted: dict[str, str] = {}

        with zipfile.ZipFile(zip_path) as zf:
            infolist = [info for info in zf.infolist() if not info.is_dir()]
            total = len(infolist)

            for idx, info in enumerate(infolist):
                name = os.path.basename(info.filename)
                out_path = os.path.join(dest_dir, name)

                self._progress_cb(
                    {
                        "type": "extract_progress",
                        "file": name,
                        "percent": int(100 * idx / max(1, total)),
                    }
                )

                with zf.open(info) as src, open(out_path, "wb") as dst:
                    while True:
                        if self._should_cancel:
                            raise CancelledError("Extract cancelled")
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)

                extracted[name] = out_path

        self._progress_cb({"type": "extract_progress", "file": None, "percent": 100})
        return extracted

    def _verify_sha256(self, path: str, expected: str) -> bool:
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    buf = f.read(1 << 20)
                    if not buf:
                        break
                    hasher.update(buf)
        except OSError:
            return False
        return hasher.hexdigest().lower() == expected.lower()

    def cleanup(self) -> None:
        """Removes the dynamically allocated temporary directory."""
        if self.temp_dir:
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception:
                pass
            self.temp_dir = None

    def _check_cache(self, expected_sha: str) -> str | None:
        """Check if valid firmware exists in local cache."""
        if not self.cache_dir or not expected_sha:
            return None

        os.makedirs(self.cache_dir, exist_ok=True)
        cache_path = os.path.join(self.cache_dir, f"{expected_sha}.zip")

        if os.path.isfile(cache_path):
            self._progress_cb(
                {"type": "prep_phase", "phase": "Verifying cached firmware"}
            )
            if self._verify_sha256(cache_path, expected_sha):
                return cache_path

            try:
                os.remove(cache_path)
            except OSError:
                pass

        return None

    def _download_and_cache(self, url: str, expected_sha: str) -> str:
        """Download firmware payload natively and attempt cache storage."""
        if not self.temp_dir:
            raise RuntimeError("Temporary directory uninitialized")

        zip_path = os.path.join(self.temp_dir, "firmware.zip")
        self._progress_cb({"type": "prep_phase", "phase": "Downloading firmware"})
        self.download_file(url, zip_path, expected_sha256=expected_sha or None)

        if self.cache_dir and expected_sha:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                target = os.path.join(self.cache_dir, f"{expected_sha}.zip")
                if os.path.exists(target):
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                shutil.move(zip_path, target)
                zip_path = target
                self._progress_cb(
                    {"type": "prep_phase", "phase": f"Cached at {target}"}
                )
            except OSError as e:
                self._progress_cb(
                    {
                        "type": "error",
                        "message": f"Could not save to cache ({self.cache_dir}): {e}",
                    }
                )

        return zip_path

    def execute(
        self, version_arg: str, force_redownload: bool = False
    ) -> tuple[str, str, bool]:
        """Main public entrypoint to resolve, download, verify and extract firmware."""
        self._progress_cb({"type": "prep_phase", "phase": "Fetching manifest"})
        channel, version, release = self.resolve_version(version_arg)

        is_fastboot = release.get("fastboot", False)

        artifact = release.get("artifacts", {}).get("firmware")
        if not artifact:
            raise ValueError("No firmware artifact in release")

        expected_sha = artifact.get("sha256", "").lower()
        self.temp_dir = tempfile.mkdtemp(prefix="smhub-fw-")
        rom_dir = os.path.join(self.temp_dir, "rom")

        zip_path = None
        if not force_redownload:
            zip_path = self._check_cache(expected_sha)

        try:
            if zip_path:
                self._progress_cb(
                    {
                        "type": "prep_phase",
                        "phase": "Using cached firmware (skipped download)",
                    }
                )
            else:
                zip_path = self._download_and_cache(artifact["url"], expected_sha)

            self._progress_cb({"type": "prep_phase", "phase": "Extracting"})
            self.extract_rom_files(zip_path, rom_dir)

            # Subclasses can implement a final _cleanup_pbar method if needed
            self._progress_cb({"type": "prep_phase", "phase": "Done"})

            return rom_dir, self.temp_dir, is_fastboot

        except Exception:
            self.cleanup()
            self._progress_cb({"type": "prep_phase", "phase": "Error"})
            raise

    @staticmethod
    def list_cache(cache_dir: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not os.path.isdir(cache_dir):
            return out
        for name in os.listdir(cache_dir):
            p = os.path.join(cache_dir, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p) and name.endswith(".zip"):
                out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    @staticmethod
    def clear_cache(cache_dir: str) -> int:
        if not os.path.isdir(cache_dir):
            return 0
        removed = 0
        for name in os.listdir(cache_dir):
            p = os.path.join(cache_dir, name)
            if os.path.isfile(p) and name.endswith(".zip"):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        return removed


@dataclass
class ProgressBarModel:
    """Data model representing the state and configuration of a progress bar."""

    units: str = "B"
    unit_scale: bool = False
    description: str | None = None


class CliFirmwareDownloader(FirmwareDownloader):
    """Terminal-specific subclass that routes _progress_cb through strict tqdm updates."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        manifest_url: str = MANIFEST_URL_DEFAULT,
        cache_dir: str | None = None,
    ) -> None:
        super().__init__(logger, manifest_url, cache_dir)
        self.pbar: Any = None
        self.current_op: str | None = None
        from rich.console import Console

        self.console = Console()
        self.last_phase: str | None = None

    def _apply_pbar_model(self, model: ProgressBarModel, total: int) -> None:
        from tqdm import tqdm

        if self.pbar is None:
            self.pbar = tqdm(
                total=total,
                desc=f"  ↑  {model.description}",
                unit=model.units,
                unit_scale=model.unit_scale,
                leave=False,
            )
        else:
            self.pbar.unit = model.units
            self.pbar.unit_scale = model.unit_scale
            if model.description:
                self.pbar.set_description(model.description)
            self.pbar.reset(total=total)

    def _progress_cb(self, info: dict[str, Any]) -> None:
        typ = info.get("type")
        from . import events

        if typ == "download_progress":
            if self.current_op != "download":
                self.current_op = "download"
                self._apply_pbar_model(
                    ProgressBarModel(
                        units="B", unit_scale=True, description="Downloading"
                    ),
                    info.get("total", 0),
                )
            if self.pbar:
                self.pbar.n = info.get("downloaded", 0)
                self.pbar.refresh()
                events.emit_tqdm_progress(self.pbar, "Downloading")
        elif typ == "extract_progress":
            if self.current_op != "extract":
                self.current_op = "extract"
                self._apply_pbar_model(
                    ProgressBarModel(
                        units="%", unit_scale=False, description="Extracting"
                    ),
                    100,
                )
            if self.pbar:
                self.pbar.n = info.get("percent", 0)
                self.pbar.refresh()
                if events.JSON_FD_OBJ is not None:
                    events.emit(
                        type="progress",
                        percent=info.get("percent", 0),
                        current=str(info.get("percent", 0)),
                        total="100",
                        label=info.get("file") or "Extracting",
                        remaining="",
                    )
        elif typ == "prep_phase":
            phase = info.get("phase")
            if events.JSON_FD_OBJ is not None:
                events.emit(type="prep_phase", phase=phase)

            if self.pbar:
                self.pbar.close()
                self.pbar = None
                self.current_op = None

            if self.last_phase:
                import sys

                sys.stdout.write("\033[F\033[K")
                sys.stdout.flush()
                if phase == "Error":
                    self.console.print(f"  [red]✗[/red]  {self.last_phase}")
                else:
                    self.console.print(f"  [green]✓[/green]  {self.last_phase}")
                self.last_phase = None

            if phase not in ("Done", "Error"):
                self.last_phase = phase
                self.console.print(f"  [yellow]⠋[/yellow]  {phase}")
