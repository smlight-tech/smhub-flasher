# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import asyncio
import logging
import struct
import sys
from array import array
from collections.abc import Callable, Sequence
from typing import Any

import fastcrc
import usb.core
import usb.util

from .exceptions import UsbPermissionError

logger = logging.getLogger(__name__)

TRACE = 5
logging.addLevelName(TRACE, "TRACE")
SUCCESS = 0
FAIL = 1
TIMEOUT = -1
HEADER_SIZE = 8
MSG_TOKEN_OFFSET = 0
CV_USB_KEEP_DL = 3
CV_USB_NONE = 0
DUMMY_ADDR = 0xFF
CVI_USB_PROGRAM = 0x83
CV_USB_UBREAK = 4
CVI_USB_TX_FLAG = 1
CV_USB_BREAK = 2
CVI_USB_REBOOT = 22

# Flag values written to BOOT_SOURCE_FLAG_ADDR (0x0E000004) via CVI_USB_TX_FLAG.
# U-Boot reads this in misc_init_r:
#   "1NGM" (USB_DL_FLAG_NORMAL) → normal boot (CVI flash) or enter fastboot mode depending on U-Boot fork
USB_DL_FLAG_NORMAL = array("B", [ord(c) for c in "1NGM"])


class UsbTransport:
    def __init__(self, vid: int, pid: int) -> None:
        self.vid: int = vid
        self.pid: int = pid
        self.device: usb.core.Device | None = None
        self.ep_in: usb.core.Endpoint | None = None
        self.ep_out: usb.core.Endpoint | None = None
        self.intf_number: int | None = None
        self.last_ack_packet: Sequence[int] | None = None

    @staticmethod
    def probe_access(vid: int, pid: int) -> None:
        """Try to read the device descriptor; raise UsbPermissionError on EACCES.

        Safe to call before handing off to an external tool (e.g. fastboot) — reads
        only the already-enumerated configuration descriptor, never claims an interface.
        """
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            return
        try:
            _ = dev[0]
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 13:
                raise UsbPermissionError(
                    f"USB access denied for {vid:04x}:{pid:04x}"
                ) from e

    def connect_sync(self) -> None:
        """Synchronously find and configure device."""
        if self.device is None:
            self.device = usb.core.find(idVendor=self.vid, idProduct=self.pid)
            if self.device is None:
                raise RuntimeError(
                    f"Device {self.vid:04x}:{self.pid:04x} not found by pyusb."
                )

        if sys.platform == "linux":
            try:
                if self.device.is_kernel_driver_active(1):
                    self.device.detach_kernel_driver(1)
                    logger.debug("Detached kernel driver")
            except NotImplementedError:
                pass
            except usb.USBError as e:
                if getattr(e, "errno", None) == 13:
                    raise UsbPermissionError(
                        f"USB access denied for {self.vid:04x}:{self.pid:04x}"
                    ) from e
                logger.warning(f"Failed to detach kernel driver: {e}")

        try:
            cfg = self.device[0]
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 13:
                raise UsbPermissionError(
                    f"USB access denied for {self.vid:04x}:{self.pid:04x}"
                ) from e
            raise RuntimeError(
                f"Failed to retrieve device configuration descriptors: {e}"
            )

        self.ep_in = None
        self.ep_out = None

        for intf in cfg:
            local_in = None
            local_out = None
            for ep in intf:
                if (
                    usb.util.endpoint_type(ep.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK
                ):
                    direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                    if direction == usb.util.ENDPOINT_OUT:
                        local_out = ep
                    elif direction == usb.util.ENDPOINT_IN:
                        local_in = ep
            if local_in is not None and local_out is not None:
                self.ep_in = local_in
                self.ep_out = local_out
                self.intf_number = intf.bInterfaceNumber
                break

        if not self.ep_out or not self.ep_in:
            raise RuntimeError("Could not find Bulk IN/OUT endpoints")

        if self.intf_number is not None:
            self._claimed_intf = None
            try:
                usb.util.claim_interface(self.device, self.intf_number)
                self._claimed_intf = self.intf_number
                logger.debug(f"Claimed interface {self.intf_number}")
            except usb.USBError as e:
                logger.debug(f"claim_interface({self.intf_number}) failed: {e}")
                self._claimed_intf = None

            self._open_cdc_line()

        else:
            self._claimed_intf = None

    def _open_cdc_line(self) -> None:
        """Send CDC line coding and control state to open the virtual serial port."""
        if sys.platform == "linux" or self.device is None or self.intf_number is None:
            logger.debug(
                "Skipping CDC line open (not required on Linux, or missing device info)"
            )
            return

        try:
            CDC_SET_LINE_CODING = 0x20
            CDC_SET_CONTROL_LINE_STATE = 0x22
            line_coding = struct.pack("<IBBB", 115200, 0, 0, 8)
            self.device.ctrl_transfer(
                0x21,
                CDC_SET_LINE_CODING,
                0,
                self.intf_number,
                line_coding,
                timeout=1000,
            )
            logger.debug("CDC SET_LINE_CODING sent")
            self.device.ctrl_transfer(
                0x21,
                CDC_SET_CONTROL_LINE_STATE,
                0x0003,
                self.intf_number,
                b"",
                timeout=1000,
            )
            logger.debug("CDC SET_CONTROL_LINE_STATE sent (DTR=1 RTS=1)")
        except Exception as e:
            logger.debug(f"CDC open-line requests failed: {e}")

    async def connect(self) -> None:
        if sys.platform in ("darwin", "win32"):
            await asyncio.to_thread(self.connect_sync)
        else:
            self.connect_sync()

    def close(self) -> None:
        if self.device:
            try:
                claimed = getattr(self, "_claimed_intf", None)
                if claimed is not None:
                    usb.util.release_interface(self.device, claimed)
            except Exception as e:
                logger.debug(f"release_interface failed: {e}")
            usb.util.dispose_resources(self.device)
            self.device = None

    def _crc16(self, hex_data: bytes | bytearray | list[int] | Sequence[int]) -> int:
        return int(fastcrc.crc16.xmodem(bytes(hex_data)))

    def _write_sync(
        self, command: bytes | bytearray, recv_ack: bool = True, timeout: int = 5000
    ) -> int:
        if self.ep_out is None or self.ep_in is None:
            logger.error("Endpoints not configured")
            return FAIL

        if logger.isEnabledFor(TRACE):
            debug_head = command[:16]
            logger.log(
                TRACE, f"USB OUT (Tx): {debug_head.hex()}... ({len(command)} bytes)"
            )

        try:
            self.ep_out.write(command, timeout)
        except usb.USBError as e:
            logger.debug(f"USB Write Error (ep_out): {e}")

            if self._is_usb_disconnect_error(e):
                # Abort instantly on physical disconnects or broken pipes
                # to allow the FSM to re-enumerate immediately.
                raise RuntimeError(
                    f"USB device disconnected or pipe died mid-transfer ({e})"
                )

            return FAIL

        if not recv_ack:
            return SUCCESS

        cmd_crc = self._crc16(command)

        try:
            ret = self.ep_in.read(16, timeout=timeout)
        except usb.USBError as e:
            logger.error(f"USB Read ACK Error (ep_in): {e}")
            return FAIL

        if len(ret) >= 4:
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, f"USB IN (Rx ACK): {bytes(ret[:16]).hex()}")

            ret_crc = (ret[2] * 256) + ret[3]
            if ret_crc == cmd_crc:
                self.last_ack_packet = ret
                return SUCCESS
            else:
                logger.error(
                    f"CRC Error: expected {cmd_crc:04x}, got {ret_crc:04x} (Full Rx: {bytes(ret).hex()})"
                )
                return FAIL
        logger.error(f"Invalid ACK length received: {len(ret)}")
        return FAIL

    def _read_data_sync(self, length: int, timeout: int = 5000) -> Any:
        if self.ep_in is None:
            return None
        try:
            return self.ep_in.read(length, timeout=timeout)
        except usb.USBError as e:
            logger.error(f"Read Data Error (ep_in): {e}")
            return None

    def _is_usb_disconnect_error(self, err: Exception | None) -> bool:
        if err is None:
            return False
        errno = getattr(err, "errno", None)
        msg = str(err).lower()
        if errno in (5, 19, 32):
            return True
        return (
            "no such device" in msg
            or "disconnected" in msg
            or "has been disconnected" in msg
            or "pipe" in msg
        )

    def _send_req_data_sync(
        self,
        token: int,
        address: int,
        req_len: int,
        ack: bool = True,
        data: bytes | bytearray | array[int] | None = None,
        timeout: int = 5000,
    ) -> Any:
        if self.ep_out is None or self.ep_in is None:
            logger.error("Endpoints not configured")
            return None

        # Pack precisely to an 8-byte buffer.
        # >B H B I expands to: token(1) size_lo(2) addr_hi(1) addr_lo(4) = 8 bytes
        addr_hi = (address >> 32) & 0xFF
        addr_lo = address & 0xFFFFFFFF

        # original header swapped bytes slightly if req_len>0xFFFF, assuming simple layout:
        # B (token), H (size low), B (address high), I (address low)
        cmd_bytes = struct.pack(">BHBI", token, req_len & 0xFFFF, addr_hi, addr_lo)
        cmd = bytearray(cmd_bytes)

        if data:
            cmd.extend(data)

        if ack:
            cmd_crc = self._crc16(cmd)
            try:
                self.ep_out.write(cmd, timeout=timeout)
                rsp = self.ep_in.read(16, timeout=timeout)

                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        f"USB REQ OUT: {bytes(cmd).hex()} | IN: {bytes(rsp).hex()}",
                    )

                # CVI_PROGRAM/REBOOT does not ACK normally with CRC
                if token in [CVI_USB_PROGRAM, CVI_USB_REBOOT]:
                    return rsp

                ret_crc = (rsp[2] * 256) + rsp[3]
                if ret_crc == cmd_crc:
                    return rsp
                logger.error(
                    f"ACK_CRC_ERROR on req_data: expected {cmd_crc:04x}, got {ret_crc:04x} (Full Rx: {bytes(rsp).hex()})"
                )
            except Exception as e:
                logger.error(f"Req data IO failed: {e}")
            return None
        else:
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, f"USB REQ OUT (No ACK): {bytes(cmd).hex()}")
            try:
                self.ep_out.write(cmd, timeout=timeout)
            except Exception:
                pass
            return None

    def _send_chunk_sync(
        self,
        data_bytes: bytes,
        dest_addr: int,
        is_magic: bool = False,
        timeout: int = 5000,
    ) -> int:
        tx_len = len(data_bytes) + HEADER_SIZE
        token = CV_USB_KEEP_DL if is_magic else CV_USB_NONE

        addr_hi = (dest_addr >> 32) & 0xFF
        addr_lo = dest_addr & 0xFFFFFFFF

        # >B H B I expands to: token(1) tx_len(2) addr_hi(1) addr_lo(4) = 8 bytes
        header_bytes = struct.pack(">BHBI", token, tx_len & 0xFFFF, addr_hi, addr_lo)

        cmd = bytearray(header_bytes)
        cmd.extend(data_bytes)

        return self._write_sync(cmd, recv_ack=True, timeout=timeout)

    async def send_req_data(
        self,
        token: int,
        address: int,
        req_len: int,
        ack: bool = True,
        data: bytes | bytearray | array[int] | None = None,
        timeout: int = 5000,
    ) -> Any:
        return await asyncio.to_thread(
            self._send_req_data_sync, token, address, req_len, ack, data, timeout
        )

    async def write(self, command: bytes | bytearray, recv_ack: bool = True) -> int:
        return await asyncio.to_thread(self._write_sync, command, recv_ack)

    async def read_data(self, length: int, timeout: int = 5000) -> Any:
        return await asyncio.to_thread(self._read_data_sync, length, timeout)

    async def send_file_chunked(
        self,
        file_path: str,
        dest_addr: int,
        is_magic: bool = False,
        chunk_size: int = 4096,
        max_bytes: int | None = None,
        start_offset: int = 0,
        progress_callback: Callable[[int, int], None] | None = None,
        progress_base: int = 0,
        progress_total: int | None = None,
    ) -> int:
        """Sends a file over USB in chunks"""

        def run_sender() -> int:
            with open(file_path, "rb") as f:
                f.seek(start_offset)
                bytes_sent = 0
                addr = dest_addr
                last_progress_emit = -1

                while True:
                    if max_bytes is not None and bytes_sent >= max_bytes:
                        break

                    read_size = chunk_size - HEADER_SIZE
                    if max_bytes is not None:
                        read_size = min(read_size, max_bytes - bytes_sent)

                    buf = f.read(read_size)
                    if not buf:
                        break

                    ret = self._send_chunk_sync(
                        buf,
                        addr,
                        is_magic=is_magic,
                        timeout=5000,
                    )

                    if ret != SUCCESS:
                        raise RuntimeError(
                            f"Failed to send chunk at address {addr:08x}"
                        )

                    addr += len(buf)
                    bytes_sent += len(buf)

                    if progress_callback and progress_total:
                        done = progress_base + bytes_sent
                        bucket = done // 4096
                        if bucket != last_progress_emit:
                            last_progress_emit = bucket
                            progress_callback(done, progress_total)
            return bytes_sent

        return await asyncio.to_thread(run_sender)
