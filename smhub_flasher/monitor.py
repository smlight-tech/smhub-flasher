# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from typing import Protocol

import usb.core

logger = logging.getLogger(__name__)


class UsbMonitorBase(Protocol):
    event_queue: asyncio.Queue[tuple[str, int, int, str]]

    def __init__(self, target_vids: list[int], target_pids: list[int]) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    async def wait_for_device(
        self,
        actions: tuple[str, ...] = ("add",),
        vid: int | None = None,
        pid: int | None = None,
    ) -> tuple[str, int, int, str]: ...


class PollingUsbMonitor:
    def __init__(self, target_vids: list[int], target_pids: list[int]) -> None:
        self.target_vids: list[int] = target_vids
        self.target_pids: list[int] = target_pids
        self.event_queue: asyncio.Queue[tuple[str, int, int, str]] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = 0.2

    def _snapshot(self) -> set[tuple[int, int, int, int]]:
        seen: set[tuple[int, int, int, int]] = set()
        try:
            devs = list(usb.core.find(find_all=True))
            for dev in devs:
                vid = dev.idVendor
                pid = dev.idProduct
                if vid in self.target_vids and pid in self.target_pids:
                    # Windows Plug-and-Play is slow to bind the WinUSB driver.
                    # Do not emit the device until the descriptors are actually readable.
                    if sys.platform == "win32":
                        try:
                            _ = dev[0]
                        except usb.core.USBError:
                            continue
                    seen.add((vid, pid, dev.bus, dev.address))
        except Exception as e:
            logger.error(f"USB poll error: {e}")
        return seen

    def _run(self) -> None:
        prev = self._snapshot()
        while not self._stop.is_set():
            time.sleep(self._interval)
            curr = self._snapshot()
            for key in curr - prev:
                vid, pid, _bus, _addr = key
                logger.debug(f"USB Mon: add - {vid:04x}:{pid:04x}")
                self.loop.call_soon_threadsafe(
                    self.event_queue.put_nowait, ("add", vid, pid, "")
                )
            for key in prev - curr:
                vid, pid, _bus, _addr = key
                logger.debug(f"USB Mon: remove - {vid:04x}:{pid:04x}")
                self.loop.call_soon_threadsafe(
                    self.event_queue.put_nowait, ("remove", vid, pid, "")
                )
            prev = curr

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("USB Monitor started (polling backend)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("USB Monitor stopped")

    async def wait_for_device(
        self,
        actions: tuple[str, ...] = ("add",),
        vid: int | None = None,
        pid: int | None = None,
    ) -> tuple[str, int, int, str]:
        while True:
            action, e_vid, e_pid, node = await self.event_queue.get()
            if action not in actions:
                continue
            if vid is not None and e_vid != vid:
                continue
            if pid is not None and e_pid != pid:
                continue
            if action == "add":
                present = False
                try:
                    for dev in usb.core.find(find_all=True):
                        if dev.idVendor == e_vid and dev.idProduct == e_pid:
                            present = True
                            break
                except Exception:
                    present = True
                if not present:
                    logger.debug(
                        f"Discarding stale add event for {e_vid:04x}:{e_pid:04x}"
                    )
                    continue
            return action, e_vid, e_pid, node


if sys.platform == "linux":
    import pyudev

    class LinuxUsbMonitor:
        def __init__(self, target_vids: list[int], target_pids: list[int]) -> None:
            self.target_vids: list[int] = target_vids
            self.target_pids: list[int] = target_pids
            self.event_queue: asyncio.Queue[tuple[str, int, int, str]] = asyncio.Queue()
            self.context: pyudev.Context = pyudev.Context()
            self.monitor: pyudev.Monitor = pyudev.Monitor.from_netlink(self.context)
            self.monitor.filter_by(subsystem="usb", device_type="usb_device")
            self._observer: pyudev.MonitorObserver | None = None
            self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

        def _event_handler(self, action: str, device: pyudev.Device) -> None:
            vid_str: str | None = device.get("ID_VENDOR_ID")
            pid_str: str | None = device.get("ID_MODEL_ID")

            if vid_str and pid_str:
                try:
                    vid = int(vid_str, 16)
                    pid = int(pid_str, 16)

                    if vid in self.target_vids and pid in self.target_pids:
                        logger.debug(f"USB Mon: Device {action} - {vid:04x}:{pid:04x}")
                        self.loop.call_soon_threadsafe(
                            self.event_queue.put_nowait,
                            (action, vid, pid, device.device_node),
                        )
                except ValueError:
                    pass

        def start(self) -> None:
            self._observer = pyudev.MonitorObserver(self.monitor, self._event_handler)
            self._observer.start()
            logger.info("USB Monitor started (udev backend)")

        def stop(self) -> None:
            if self._observer:
                self._observer.stop()
                logger.info("USB Monitor stopped")

        async def wait_for_device(
            self,
            actions: tuple[str, ...] = ("add",),
            vid: int | None = None,
            pid: int | None = None,
        ) -> tuple[str, int, int, str]:
            while True:
                action, e_vid, e_pid, node = await self.event_queue.get()
                if action in actions:
                    if (vid is None or e_vid == vid) and (pid is None or e_pid == pid):
                        return action, e_vid, e_pid, node

    UsbMonitor = LinuxUsbMonitor
else:
    UsbMonitor = PollingUsbMonitor  # type: ignore[misc]
