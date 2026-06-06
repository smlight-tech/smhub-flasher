# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import asyncio
from unittest.mock import MagicMock, patch
import pytest
import usb.core

from smhub_flasher.monitor import PollingUsbMonitor


class MockUsbDevice:
    def __init__(self, vid: int, pid: int, bus: int, address: int) -> None:
        self.idVendor = vid
        self.idProduct = pid
        self.bus = bus
        self.address = address
        self._ctx = MagicMock()

    def __getitem__(self, idx: int) -> MagicMock:
        return MagicMock()


@pytest.mark.asyncio
async def test_polling_event_cycle() -> None:
    # We target ROM_IDS (0x3346, 0x1000)
    monitor = PollingUsbMonitor(target_vids=[0x3346], target_pids=[0x1000])

    # Step 1: Device is connected initially
    dev = MockUsbDevice(0x3346, 0x1000, 1, 1)

    with patch("usb.core.find") as mock_find:
        mock_find.return_value = [dev]

        # Start monitor thread
        monitor.start()

        # Allow loop to run a bit to process the first snapshot
        await asyncio.sleep(0.1)

        # wait_for_device should get the add event immediately from the queue
        action, vid, pid, node = await monitor.wait_for_device(actions=("add",))
        assert action == "add"
        assert vid == 0x3346
        assert pid == 0x1000

        # Step 2: Simulate device disconnect (reset)
        mock_find.return_value = []
        # Wait for monitor thread to poll and notice the removal
        await asyncio.sleep(0.3)

        # Step 3: Simulate device reconnects
        dev2 = MockUsbDevice(0x3346, 0x1000, 1, 2)  # new address
        mock_find.return_value = [dev2]

        # Wait for monitor to poll and queue the new add event
        await asyncio.sleep(0.3)

        # Since it disconnected and reconnected, wait_for_device should get the new add event
        action, vid, pid, node = await monitor.wait_for_device(actions=("add",))
        assert action == "add"
        assert vid == 0x3346
        assert pid == 0x1000

        # Clean up
        monitor.stop()
