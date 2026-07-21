# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import asyncio
from unittest.mock import patch

import pytest

from smhub_flasher.flasher_fsm import ROM_IDS, FlasherFSM


class SilentMonitor:
    """Monitor that never emits an event — simulates a missed hotplug edge."""

    def __init__(self) -> None:
        self.event_queue: asyncio.Queue = asyncio.Queue()

    async def wait_for_device(self, actions=("add",), vid=None, pid=None):
        return await self.event_queue.get()


def _fsm() -> FlasherFSM:
    return FlasherFSM(SilentMonitor(), fip_path="fip.bin", emmc_path="emmc.img")


@pytest.mark.asyncio
async def test_falls_back_to_presence_probe_when_no_add_event() -> None:
    """A re-enumeration missed by the poller must not block forever."""
    fsm = _fsm()

    with patch("smhub_flasher.transport.UsbTransport.is_present", return_value=True):
        vid, pid = await asyncio.wait_for(
            fsm._wait_for_usb_device(ROM_IDS, probe_after=0.1), timeout=2.0
        )

    assert (vid, pid) == ROM_IDS


@pytest.mark.asyncio
async def test_keeps_waiting_when_device_is_absent() -> None:
    """With no event and no device on the bus, the wait stays open."""
    fsm = _fsm()

    with patch("smhub_flasher.transport.UsbTransport.is_present", return_value=False):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                fsm._wait_for_usb_device(ROM_IDS, probe_after=0.1), timeout=0.5
            )


@pytest.mark.asyncio
async def test_add_event_wins_over_probe() -> None:
    """A real hotplug event is used as soon as it arrives, without probing."""
    fsm = _fsm()
    fsm.monitor.event_queue.put_nowait(("add", *ROM_IDS, ""))

    with patch(
        "smhub_flasher.transport.UsbTransport.is_present", return_value=False
    ) as probe:
        vid, pid = await asyncio.wait_for(
            fsm._wait_for_usb_device(ROM_IDS, probe_after=5.0), timeout=2.0
        )

    assert (vid, pid) == ROM_IDS
    probe.assert_not_called()
