# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import os
import sys
import tempfile
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch
import pytest
import usb.core

from smhub_flasher.exceptions import UsbPermissionError
from smhub_flasher.transport import FAIL, SUCCESS, UsbTransport


class MockEndpoint:
    def __init__(self, address: int, attributes: int) -> None:
        self.bEndpointAddress = address
        self.bmAttributes = attributes
        self.last_written_crc: int = 0

    def write(self, data: bytes | bytearray, timeout: int = 5000) -> int:
        return len(data)

    def read(self, size: int, timeout: int = 5000) -> bytearray:
        return bytearray([0] * size)


class MockInterface:
    def __init__(self, number: int, endpoints: list[MockEndpoint]) -> None:
        self.bInterfaceNumber = number
        self.endpoints = endpoints

    def __iter__(self) -> Iterator[MockEndpoint]:
        return iter(self.endpoints)


class MockConfiguration(usb.core.Configuration):  # type: ignore[misc]
    def __init__(self, interfaces: list[MockInterface]) -> None:
        self.interfaces = interfaces

    def __iter__(self) -> Iterator[MockInterface]:
        return iter(self.interfaces)


class MockUsbDevice:
    def __init__(self, vid: int, pid: int) -> None:
        self.idVendor = vid
        self.idProduct = pid
        self.bus = 1
        self.address = 1
        self._ctx = MagicMock()

        # Build mock interfaces and endpoints (1 IN, 1 OUT bulk)
        self.ep_out = MockEndpoint(0x01, usb.util.ENDPOINT_TYPE_BULK | usb.util.ENDPOINT_OUT)
        self.ep_in = MockEndpoint(0x81, usb.util.ENDPOINT_TYPE_BULK | usb.util.ENDPOINT_IN)
        self.interface = MockInterface(1, [self.ep_out, self.ep_in])
        self.cfg = MockConfiguration([self.interface])

        self.kernel_driver_active = True

    def __getitem__(self, idx: int) -> MockConfiguration:
        return self.cfg

    def is_kernel_driver_active(self, interface: int) -> bool:
        return self.kernel_driver_active

    def detach_kernel_driver(self, interface: int) -> None:
        self.kernel_driver_active = False

    def ctrl_transfer(
        self,
        bmRequestType: int,
        bRequest: int,
        wValue: int,
        wIndex: int,
        data_or_wLength: Any = None,
        timeout: int = 5000,
    ) -> int:
        return 0


@pytest.mark.asyncio
async def test_transport_connect_success() -> None:
    transport = UsbTransport(0x3346, 0x1000)
    mock_dev = MockUsbDevice(0x3346, 0x1000)

    with patch("usb.core.find") as mock_find, patch("usb.util.claim_interface") as mock_claim:
        mock_find.return_value = mock_dev

        # Test connection
        await transport.connect()

        assert transport.device is mock_dev
        assert transport.ep_out is mock_dev.ep_out
        assert transport.ep_in is mock_dev.ep_in
        assert transport.intf_number == 1
        mock_claim.assert_called_once_with(mock_dev, 1)


@pytest.mark.asyncio
async def test_transport_connect_permission_denied() -> None:
    transport = UsbTransport(0x3346, 0x1000)
    mock_dev = MockUsbDevice(0x3346, 0x1000)

    # Mock dev[0] raising Permission Denied (errno 13)
    with patch.object(MockUsbDevice, "__getitem__", side_effect=usb.core.USBError("Permission denied", errno=13)):
        with patch("usb.core.find", return_value=mock_dev), patch("usb.util.dispose_resources") as mock_dispose:
            with pytest.raises(UsbPermissionError):
                await transport.connect()

            # Verify resources are disposed of and device is reset to None
            assert transport.device is None
            mock_dispose.assert_called_once_with(mock_dev)


@pytest.mark.asyncio
async def test_send_file_chunked_success() -> None:
    transport = UsbTransport(0x3346, 0x1000)
    mock_dev = MockUsbDevice(0x3346, 0x1000)

    # Mock successful write/read ACK
    # ACK packet must contain correct CRC: bytes 2, 3 = CRC16 of written data
    def mock_write(data: bytes | bytearray, timeout: int = 5000) -> int:
        mock_dev.ep_in.last_written_crc = transport._crc16(data)
        return len(data)

    def mock_read(size: int, timeout: int = 5000) -> bytearray:
        crc = getattr(mock_dev.ep_in, "last_written_crc", 0)
        # ACK structure: token(1), size(1), crc_hi(1), crc_lo(1) + trailing zeros
        ack = bytearray([0] * size)
        ack[2] = (crc >> 8) & 0xFF
        ack[3] = crc & 0xFF
        return ack

    setattr(mock_dev.ep_out, "write", mock_write)
    setattr(mock_dev.ep_in, "read", mock_read)

    with patch("usb.core.find", return_value=mock_dev), patch("usb.util.claim_interface"):
        await transport.connect()

        # Write dummy file content
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"Hello World USB Chunk Test")
            temp_name = f.name

        try:
            bytes_sent = await transport.send_file_chunked(temp_name, 0x08000000, chunk_size=32)
            assert bytes_sent == 26
        finally:
            os.unlink(temp_name)


@pytest.mark.asyncio
async def test_send_file_chunked_transient_retry() -> None:
    transport = UsbTransport(0x3346, 0x1000)
    mock_dev = MockUsbDevice(0x3346, 0x1000)

    attempts = 0

    def mock_write(data: bytes | bytearray, timeout: int = 5000) -> int:
        nonlocal attempts
        attempts += 1
        mock_dev.ep_in.last_written_crc = transport._crc16(data)
        return len(data)

    def mock_read(size: int, timeout: int = 5000) -> bytearray:
        crc = getattr(mock_dev.ep_in, "last_written_crc", 0)
        ack = bytearray([0] * size)
        
        # Simulate a CRC mismatch on the first write attempt
        if attempts == 1:
            ack[2] = 0xAA
            ack[3] = 0xBB
        else:
            ack[2] = (crc >> 8) & 0xFF
            ack[3] = crc & 0xFF
        return ack

    setattr(mock_dev.ep_out, "write", mock_write)
    setattr(mock_dev.ep_in, "read", mock_read)

    with patch("usb.core.find", return_value=mock_dev), patch("usb.util.claim_interface"):
        await transport.connect()

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"Hello World Retry Test")
            temp_name = f.name

        try:
            bytes_sent = await transport.send_file_chunked(temp_name, 0x08000000, chunk_size=32)
            assert bytes_sent == 22
            # Should have made 2 write attempts (1 failure + 1 retry success)
            assert attempts == 2
        finally:
            os.unlink(temp_name)


@pytest.mark.asyncio
async def test_send_file_chunked_disconnect_abort() -> None:
    transport = UsbTransport(0x3346, 0x1000)
    mock_dev = MockUsbDevice(0x3346, 0x1000)

    # Mock write raising "No such device" errno 19 (disconnect error)
    def mock_write(data: bytes | bytearray, timeout: int = 5000) -> int:
        raise usb.core.USBError("No such device (it disconnected)", errno=19)

    setattr(mock_dev.ep_out, "write", mock_write)

    with patch("usb.core.find", return_value=mock_dev), patch("usb.util.claim_interface"):
        await transport.connect()

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"Hello Disconnect Test")
            temp_name = f.name

        try:
            # Should raise RuntimeError immediately without repeating attempts
            with pytest.raises(RuntimeError, match="disconnected or pipe died"):
                await transport.send_file_chunked(temp_name, 0x08000000, chunk_size=32)
        finally:
            os.unlink(temp_name)
