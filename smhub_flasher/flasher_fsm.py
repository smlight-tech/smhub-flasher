# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import asyncio
import logging
import os
import struct
import sys
from array import array
from typing import Any

from rich.console import Console

from . import events
from .monitor import UsbMonitor
from .transport import (
    CV_USB_BREAK,
    CV_USB_UBREAK,
    CVI_USB_PROGRAM,
    CVI_USB_REBOOT,
    CVI_USB_TX_FLAG,
    DUMMY_ADDR,
    USB_DL_FLAG_NORMAL,
    UsbTransport,
)

console = Console()
logger = logging.getLogger(__name__)

# BootROM and vendor U-Boot
ROM_IDS: tuple[int, int] = (0x3346, 0x1000)
# SMHUB U-Boot uses the standard Android fastboot VID:PID
FASTBOOT_IDS: tuple[int, int] = (0x18D1, 0x4EE0)


async def _spin(label: str) -> None:
    """Animate a spinner on a single line until cancelled."""
    with console.status(label, spinner="dots", spinner_style="yellow"):
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass


def _section(title: str) -> None:
    """Print a section header for a major FSM phase."""
    if events.JSON_FD_OBJ is not None:
        events.emit(type="phase", phase=title)
    console.print(f"\n[cyan bold]▶  {title}[/cyan bold]")


def _ok(label: str) -> None:
    if events.JSON_FD_OBJ is not None:
        events.emit(type="ok", message=label)
    console.print(f"  [green]✓[/green]  {label}")


def _err(label: str) -> None:
    if events.JSON_FD_OBJ is not None:
        events.emit(type="fail", message=label)
    console.print(f"  [red]✗[/red]  {label}")


def _resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller extracts `--add-data "$flasherPkg;smhub_flasher"` here
        base_path = os.path.join(sys._MEIPASS, "smhub_flasher")
    else:
        base_path = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(base_path, relative_path)


class FlasherFSM:
    def __init__(
        self,
        monitor: UsbMonitor,
        fip_path: str,
        emmc_path: str,
        img_type: str = "Unknown",
        fastboot_mode: bool = False,
        test_bootloader: bool = False,
        kernel_path: str = "",
        rootfs_path: str = "",
        slot: str = "a",
        no_bootloader: bool = True,
        bootloader_only: bool = False,
    ) -> None:
        self.monitor: UsbMonitor = monitor
        self.fip_path: str = fip_path
        self.emmc_path: str = emmc_path
        self.magic_path: str = _resource_path("cv_dl_magic.bin")
        self.img_type: str = img_type
        self.fastboot_mode: bool = fastboot_mode
        self.kernel_path: str = kernel_path
        self.rootfs_path: str = rootfs_path
        self.slot: str = slot
        self.test_bootloader: bool = test_bootloader
        self.no_bootloader: bool = no_bootloader
        self.bootloader_only: bool = bootloader_only
        self.transport: UsbTransport | None = None
        self.state: str = "INIT"
        self._fastboot_bin: str = "fastboot"

    async def run(self) -> None:
        self.state = "WAIT_ROM"

        while self.state != "DONE":
            if self.state == "WAIT_ROM":
                await self._state_wait_rom()
            elif self.state == "ROM_HANDSHAKE":
                await self._state_rom_handshake()
            elif self.state == "WAIT_UBOOT":
                await self._state_wait_uboot()
            elif self.state == "WAIT_EMMC_CONNECTION":
                await self._state_wait_emmc_connection()
            elif self.state == "FLASH_EMMC":
                await self._state_flash_emmc()
            elif self.state == "WAIT_FASTBOOT_CONNECTION":
                await self._state_wait_fastboot_connection()
            elif self.state == "FLASH_FASTBOOT":
                await self._state_flash_fastboot()
            else:
                logger.error(f"Unknown state: {self.state}")
                return

    async def _wait_for_usb_device(self, *ids: tuple[int, int]) -> tuple[int, int]:
        logger.debug(f"Waiting for USB device {[f'{v:04x}:{p:04x}' for v, p in ids]}")
        while True:
            action, e_vid, e_pid, _ = await self.monitor.wait_for_device(
                actions=("add",)
            )
            if (e_vid, e_pid) in ids:
                return e_vid, e_pid

    async def _state_wait_rom(self) -> None:
        _section("BootROM Detection")
        spinner = asyncio.create_task(
            _spin("Waiting for device to enter BootROM mode...")
        )
        vid, pid = await self._wait_for_usb_device(ROM_IDS)
        spinner.cancel()
        await asyncio.gather(spinner, return_exceptions=True)
        _ok(f"BootROM detected ({vid:04x}:{pid:04x})")
        self.transport = UsbTransport(vid, pid)
        await self.transport.connect()
        self.state = "ROM_HANDSHAKE"

    async def _state_rom_handshake(self) -> None:
        _section("BootROM Handshake")

        if not self.transport:
            raise RuntimeError("Transport not initialized")

        spinner = asyncio.create_task(_spin("Negotiating BootROM handshake..."))

        for attempt in range(3):
            try:
                magic_size = os.path.getsize(self.magic_path)
                logger.debug(f"Sending magic.bin (attempt {attempt + 1})")
                await self.transport.send_file_chunked(
                    self.magic_path,
                    DUMMY_ADDR,
                    is_magic=True,
                    chunk_size=magic_size + 8,
                )

                if attempt == 0:
                    # On the first handshake attempt, the BootROM usually resets its USB PHY
                    # and physically drops off the bus.
                    logger.debug(
                        "First magic sent, skipping FIP to wait for expected re-enumeration"
                    )
                    self.transport.close()

                    try:
                        vid, pid = await asyncio.wait_for(
                            self._wait_for_usb_device(ROM_IDS), timeout=5.0
                        )
                        self.transport = UsbTransport(vid, pid)
                        await self.transport.connect()
                    except TimeoutError:
                        logger.debug("Re-enumeration timed out")
                    continue

                logger.debug("Sending initial FIP chunk")
                await self.transport.send_file_chunked(
                    self.fip_path, 0, is_magic=False, chunk_size=512, max_bytes=4096
                )

                logger.debug("Setting 1NGM flags")
                flag = USB_DL_FLAG_NORMAL
                await self.transport.send_req_data(
                    CVI_USB_TX_FLAG, 0x0E000004, 12, ack=True, data=flag
                )

                logger.debug("Sending BREAK command")
                await self.transport.send_req_data(
                    CV_USB_BREAK, DUMMY_ADDR, 0, ack=False
                )

                self.transport.close()
                spinner.cancel()
                await asyncio.gather(spinner, return_exceptions=True)
                _ok("BootROM handshake complete")
                self.state = "WAIT_UBOOT"
                return  # Success

            except Exception as e:
                logger.debug(f"Handshake attempt {attempt + 1} failed: {e}")
                self.transport.close()

                if attempt < 2:
                    # Device will re-enumerate — wait for it and retry immediately
                    logger.debug("Waiting for device re-enumeration...")
                    try:
                        vid, pid = await asyncio.wait_for(
                            self._wait_for_usb_device(ROM_IDS), timeout=5.0
                        )
                        self.transport = UsbTransport(vid, pid)
                        await self.transport.connect()
                    except TimeoutError:
                        logger.debug("Re-enumeration timed out, going back to WAIT_ROM")
                        break

        spinner.cancel()
        await asyncio.gather(spinner, return_exceptions=True)
        _err("Handshake failed — retrying from BootROM detection")
        self.state = "WAIT_ROM"

    async def _state_wait_uboot(self) -> None:
        spinner = asyncio.create_task(_spin("Loading U-Boot FIP into DRAM..."))
        vid, pid = await self._wait_for_usb_device(ROM_IDS, FASTBOOT_IDS)
        logger.debug(f"Detected next stage device: {vid:04x}:{pid:04x}")

        if (vid, pid) == FASTBOOT_IDS:
            # If Fastboot is spotted natively here in wait uboot, skip to fastboot flash
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)
            _ok(f"U-Boot re-enumerated natively in Fastboot mode ({vid:04x}:{pid:04x})")
            if not self.fastboot_mode:
                logger.warning(
                    "  [yellow]⚠[/yellow] Warning: Device entered Fastboot, but --fastboot was not explicitly requested."
                )
            self.state = "FLASH_FASTBOOT"
            return

        if (vid, pid) == ROM_IDS:
            self.transport = UsbTransport(vid, pid)
            await self.transport.connect()
            await asyncio.sleep(0.5)

            logger.debug(
                "In Stage 2 ROM shell, repeating handshake for dynamically requested FIP chunks"
            )

            magic_size = os.path.getsize(self.magic_path)
            await self.transport.send_file_chunked(
                self.magic_path, DUMMY_ADDR, is_magic=True, chunk_size=magic_size + 8
            )

            ret = self.transport.last_ack_packet
            if ret and len(ret) >= 16:
                self.fip_tx_offset = (
                    ret[8] * (2**24) + ret[9] * (2**16) + ret[10] * (2**8) + ret[11]
                )
                self.fip_tx_size = (
                    ret[12] * (2**24) + ret[13] * (2**16) + ret[14] * (2**8) + ret[15]
                )
                logger.debug(
                    f"Stage 2 bounds -> offset: {self.fip_tx_offset}, size: {self.fip_tx_size}"
                )
            else:
                logger.debug(
                    "No FIP offsets provided by ROM, using entire file size fallback."
                )
                self.fip_tx_offset = 0
                self.fip_tx_size = os.path.getsize(self.fip_path)

            fip_req_size = self.fip_tx_size
            fip_req_offset = self.fip_tx_offset

            def _fip_progress(done: int, total_size: int) -> None:
                if os.environ.get("COLORAMA_DISABLE") == "1" or logger.isEnabledFor(
                    logging.DEBUG
                ):
                    pct = int(100 * done / total_size)
                    logger.info(f"  ·  U-Boot FIP: {done}/{total_size} bytes ({pct}%)")

                    if events.JSON_FD_OBJ is not None:

                        def format_bytes(n_bytes: float) -> str:
                            if n_bytes < 1024 * 1024:
                                return f"{n_bytes / 1024:.0f}K"
                            return f"{n_bytes / 1024 / 1024:.0f}M"

                        events.emit(
                            type="progress",
                            percent=pct,
                            current=format_bytes(done),
                            total=format_bytes(total_size),
                            label="U-Boot FIP",
                        )

            total_fip = os.path.getsize(self.fip_path)
            await self.transport.send_file_chunked(
                self.fip_path,
                0,
                is_magic=False,
                chunk_size=512,
                max_bytes=fip_req_size,
                start_offset=fip_req_offset,
                progress_callback=_fip_progress,
                progress_base=fip_req_offset,
                progress_total=total_fip,
            )

            is_final_chunk = fip_req_offset + fip_req_size >= os.path.getsize(
                self.fip_path
            )

            flag = USB_DL_FLAG_NORMAL
            logger.debug("Setting boot flag: 1NGM (required by FSBL / U-Boot Fastboot)")
            await self.transport.send_req_data(
                CVI_USB_TX_FLAG, 0x0E000004, 12, ack=True, data=flag
            )

            await self.transport.send_req_data(CV_USB_BREAK, DUMMY_ADDR, 0, ack=False)

            self.transport.close()

            if is_final_chunk:
                spinner.cancel()
                await asyncio.gather(spinner, return_exceptions=True)
                _ok("U-Boot FIP loaded into DRAM")
                if self.test_bootloader:
                    _ok("Test bootloader requested. Exiting without flashing.")
                    self.state = "DONE"
                    return
                if self.fastboot_mode:
                    self.state = "WAIT_FASTBOOT_CONNECTION"
                else:
                    self.state = "WAIT_EMMC_CONNECTION"
            else:
                # More chunks needed — spinner keeps running into next iteration
                spinner.cancel()
                await asyncio.gather(spinner, return_exceptions=True)
                self.state = "WAIT_UBOOT"
        else:
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)
            self.state = "FLASH_EMMC"

    async def _state_wait_emmc_connection(self) -> None:
        spinner = asyncio.create_task(_spin("Waiting for U-Boot CVI Listener..."))
        vid, pid = await self._wait_for_usb_device(ROM_IDS)
        spinner.cancel()
        await asyncio.gather(spinner, return_exceptions=True)
        _ok(f"U-Boot interface connected ({vid:04x}:{pid:04x})")
        await asyncio.sleep(0.5)
        self.transport = UsbTransport(vid, pid)
        await self.transport.connect()
        self.state = "FLASH_EMMC"

    async def _state_wait_fastboot_connection(self) -> None:
        spinner = asyncio.create_task(_spin("Waiting for U-Boot Fastboot interface..."))
        vid, pid = await self._wait_for_usb_device(FASTBOOT_IDS)
        spinner.cancel()
        await asyncio.gather(spinner, return_exceptions=True)
        _ok(f"Fastboot interface connected ({vid:04x}:{pid:04x})")
        if sys.platform == "win32":
            await asyncio.sleep(1.0)

        self._resolve_fastboot_bin()

        popen_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if sys.platform == "win32":
            import subprocess as _sp

            popen_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW

        try:
            process = await asyncio.create_subprocess_exec(
                self._fastboot_bin, "devices", **popen_kwargs
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3.0)
            lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
            for line in lines:
                parts = line.split()
                if parts and "fastboot" in parts:
                    logger.info(f"  [cyan]ℹ[/cyan]  Device Serial: {parts[0]}")
        except Exception:
            pass

        self.state = "FLASH_FASTBOOT"

    async def _send_cvi_update_query(self) -> Any:
        # CV_USB_D2S (0x82). Header only, no dataSize.
        # U-Boot returns 8 bytes (IMG_ADDR) in response.
        if not self.transport:
            return None
        await self.transport.send_req_data(0x82, 0, 16, ack=False)
        return await self.transport.read_data(8, timeout=5000)

    async def _send_cvi_update_s2d_chunk(
        self, dest_addr: int, chunk_data: bytes | bytearray
    ) -> None:
        if not self.transport:
            raise RuntimeError("Transport dropped")

        req_size = 16
        # CV_USB_S2D (0x81), req_size, dest_addr_hi, dest_addr_lo
        header = struct.pack(
            ">BHBI", 0x81, req_size, (dest_addr >> 32) & 0xFF, dest_addr & 0xFFFFFFFF
        )

        # struct msg_s2d->size is little-endian 8-bytes
        msg = bytearray(header)
        msg.extend(struct.pack("<Q", len(chunk_data)))

        ret = await self.transport.write(msg, recv_ack=True)
        if ret != 0:  # SUCCESS = 0
            raise RuntimeError(
                f"S2D header ACK failed at 0x{dest_addr:08x} (CRC mismatch or write error)"
            )
        await self.transport.write(chunk_data, recv_ack=False)

    async def _state_flash_emmc(self) -> None:
        _section("eMMC Flash (CVI)")

        if self.img_type == "Android Sparse":
            logger.warning(
                "  [yellow]⚠[/yellow] Warning: Device is in Raw CVI Mode, but the image is Android Sparse!"
            )
            logger.warning(
                "     This may fail to flash or cause boot issues if U-Boot doesn't support sparse extraction natively."
            )

        try:
            logger.debug("Querying U-Boot CVI_UPDATE for IMG_ADDR")
            recvbuf = await self._send_cvi_update_query()
            if not recvbuf or not self.transport:
                _err("Failed to receive image address from device")
                self.state = "DONE"
                return

            image_addr = int.from_bytes(recvbuf[0:8], byteorder="little")
            logger.debug(f"Target memory address (IMG_ADDR): 0x{image_addr:08x}")

            spinner = asyncio.create_task(
                _spin("Uploading bootloader to U-Boot RAM...")
            )
            with open(self.fip_path, "rb") as f:
                fip_data = f.read()
                s2d_chunk_len = 512 * 1024
                tmp_addr = image_addr
                for i in range(0, len(fip_data), s2d_chunk_len):
                    s2d_chunk = fip_data[i : i + s2d_chunk_len]
                    await self._send_cvi_update_s2d_chunk(tmp_addr, s2d_chunk)
                    tmp_addr += len(s2d_chunk)
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)
            _ok("Bootloader uploaded to RAM")

            spinner = asyncio.create_task(_spin("Burning boot partition (~3s)..."))
            await self.transport.send_req_data(CV_USB_UBREAK, 0x04003000, 0, ack=False)
            self.transport.close()

            # Wait for U-Boot to spin 'cvi_utask' back up after writing the boot partition
            try:
                vid, pid = await asyncio.wait_for(
                    self._wait_for_usb_device(ROM_IDS),
                    timeout=15.0,
                )
            except TimeoutError:
                raise RuntimeError("U-Boot failed to return after boot partition burn.")
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)
            _ok(f"Boot partition written — device ready ({vid:04x}:{pid:04x})")

            logger.debug(
                f"U-Boot re-enumerated ({vid:04x}:{pid:04x}), starting EMMC streaming..."
            )
            self.transport = UsbTransport(vid, pid)
            await self.transport.connect()
            await asyncio.sleep(0.5)

            recvbuf = await self._send_cvi_update_query()
            image_addr = int.from_bytes(recvbuf[0:8], byteorder="little")

            emmc_size = os.path.getsize(self.emmc_path)
            size_str = f"setenv filesize {emmc_size:x}"
            cmd = array("B", [ord(c) for c in size_str])
            await self.transport.send_req_data(
                6, 0, len(cmd) + 8, ack=True, data=cmd
            )  # PRG_CMD

            logger.debug("Sending EMMC image payload...")
            with open(self.emmc_path, "rb") as f:
                header_magic = f.read(4)
                if header_magic == b"CIMG":
                    logger.debug("Skipping global 64-byte CIMG file header")
                    f.seek(64)
                    payload_size = emmc_size - 64
                else:
                    f.seek(0)
                    payload_size = emmc_size

                max_payload_len = 16 * 1024 * 1024
                chunk_header_len = 64
                read_len = max_payload_len + chunk_header_len

                from tqdm import tqdm

                tqdm_file = sys.stdout
                if events.JSON_FD_OBJ is not None:
                    tqdm_file = open(os.devnull, "w", encoding="utf-8")

                with tqdm(
                    total=payload_size,
                    unit="B",
                    unit_scale=True,
                    desc="  ↑  eMMC image",
                    leave=True,
                    file=tqdm_file,
                    bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                ) as pbar:
                    while True:
                        full_buf = f.read(read_len)
                        if not full_buf:
                            break

                        s2d_chunk_len = 512 * 1024
                        tmp_addr = image_addr

                        for i in range(0, len(full_buf), s2d_chunk_len):
                            s2d_chunk = full_buf[i : i + s2d_chunk_len]
                            await self._send_cvi_update_s2d_chunk(tmp_addr, s2d_chunk)
                            tmp_addr += len(s2d_chunk)

                        await self.transport.send_req_data(
                            CVI_USB_PROGRAM, 0x04003000, 0, ack=True, timeout=60000
                        )

                        pbar.update(len(full_buf))
                        events.emit_tqdm_progress(pbar, "eMMC image")

            _section("Rebooting")
            spinner = asyncio.create_task(_spin("Rebooting into Linux..."))
            await asyncio.sleep(0.5)
            await self.transport.send_req_data(CVI_USB_REBOOT, 0x04003000, 0, ack=False)
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)
            _ok("eMMC image flashed — rebooting SMHUB")
            self.state = "DONE"

        except Exception as e:
            _err(f"eMMC flash failed: {e}")
            logger.exception(e)
            self.state = "DONE"

    def _resolve_fastboot_bin(self) -> bool:
        """Locate the fastboot binary; stores result in self._fastboot_bin. Returns False if not found."""
        import shutil

        exe_name = "fastboot.exe" if sys.platform == "win32" else "fastboot"
        bundled = _resource_path(exe_name)

        if os.path.exists(bundled):
            self._fastboot_bin = bundled
        elif shutil.which(exe_name):
            self._fastboot_bin = exe_name
        else:
            _err(f"Could not find {exe_name} in bundle or PATH.")
            return False
        return True

    async def _run_fastboot(
        self, args: list[str], description: str, total_size: int = 0, title: str = ""
    ) -> bool:
        """Run a fastboot sub-command, showing a spinner or tqdm progress bar."""
        import re

        from tqdm import tqdm

        spinner = None
        if total_size == 0:
            spinner = asyncio.create_task(_spin(description))
        # else: tqdm progress bar will be shown below — no extra line needed

        popen_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if sys.platform == "win32":
            import subprocess as _sp

            popen_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW

        process = await asyncio.create_subprocess_exec(
            self._fastboot_bin, *args, **popen_kwargs
        )

        pbar = None
        if total_size > 0:
            tqdm_file = sys.stdout
            if events.JSON_FD_OBJ is not None:
                tqdm_file = open(os.devnull, "w", encoding="utf-8")
            pbar = tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                desc=f"  ↑  {title}",
                leave=True,
                dynamic_ncols=True,
                disable=False,
                file=tqdm_file,
                mininterval=0.1,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
            )

        send_re = re.compile(r"Sending(?: sparse)? '.*?'(?: \d+/\d+)? \(([\d]+) KB\)")
        error_log = []

        while True:
            if not process.stdout:
                break
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            out_str = line_bytes.decode("utf-8", errors="replace").strip()
            if out_str:
                error_log.append(out_str)
                match = send_re.search(out_str)
                if pbar and match:
                    pbar.update(int(match.group(1)) * 1024)
                    events.emit_tqdm_progress(pbar, title)
                if events.JSON_FD_OBJ is not None:
                    logger.debug(f"      [dim]{out_str}[/dim]")
                elif logger.getEffectiveLevel() <= logging.DEBUG:
                    if pbar:
                        pbar.write(f"      {out_str}")
                    else:
                        logger.debug(f"      [dim]{out_str}[/dim]")

        await process.wait()

        if pbar:
            pbar.close()
        if spinner:
            spinner.cancel()
            await asyncio.gather(spinner, return_exceptions=True)

        if process.returncode != 0:
            _err(f"Fastboot command failed (exit code {process.returncode})")
            for err_line in error_log:
                logger.error(f"      [fastboot] {err_line}")
            return False

        if spinner:
            _ok(description)
        return True

    async def _fastboot_getvar(self, var: str) -> str | None:
        """Run `fastboot getvar <var>` and return the trimmed value, or None on failure."""
        try:
            popen_kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.STDOUT,
            }
            if sys.platform == "win32":
                import subprocess as _sp

                popen_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW
            proc = await asyncio.create_subprocess_exec(
                self._fastboot_bin, "getvar", var, **popen_kwargs
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            for line in out.decode("utf-8", errors="replace").splitlines():
                if var in line:
                    val = line.split(":")[-1].strip().lower()
                    return val if val else None
        except Exception as e:
            logger.debug(f"getvar {var} failed: {e}")
        return None

    async def _fastboot_flash_bootloader(self) -> bool:
        """Flash FIP to mmc0boot0. Returns False on failure."""
        return await self._run_fastboot(
            ["flash", "mmc0boot0", self.fip_path],
            "Flashing bootloader to 'mmc0boot0' partition",
        )

    async def _fastboot_resolve_slot(self) -> str:
        """Return the concrete target slot letter ('a' or 'b') based on self.slot."""
        if self.slot not in ("auto", "other"):
            return self.slot

        queried = await self._fastboot_getvar("current-slot")
        if queried in ("a", "b"):
            if self.slot == "other":
                target = "b" if queried == "a" else "a"
                logger.info(
                    f"  [cyan]ℹ[/cyan]  Active slot: [yellow]{queried.upper()}[/yellow]"
                    f" — targeting inactive slot [cyan]{target.upper()}[/cyan]"
                )
            else:  # auto
                target = queried
                logger.info(
                    f"  [cyan]ℹ[/cyan]  Targeting active slot [cyan]{target.upper()}[/cyan]"
                )
            return target

        logger.warning(
            "  [yellow]⚠[/yellow]  Could not query current-slot, defaulting to slot A"
        )
        return "a"

    async def _fastboot_flash_slot(self) -> bool:
        """Flash kernel and/or rootfs to the resolved slot. Returns False on failure."""
        target_slot = await self._fastboot_resolve_slot()
        # Slot a → index 0 (KERNEL0/ROOTFS0), slot b → index 1 (KERNEL1/ROOTFS1)
        slot_idx = "0" if target_slot == "a" else "1"

        if self.kernel_path:
            kernel_part = f"KERNEL{slot_idx}"
            if not await self._run_fastboot(
                ["flash", kernel_part, self.kernel_path],
                f"Flashing kernel image to '{kernel_part}' partition",
                total_size=os.path.getsize(self.kernel_path),
                title=kernel_part,
            ):
                return False

        if self.rootfs_path:
            with open(self.rootfs_path, "rb") as _rf:
                _hdr = _rf.read(4)
                _rf.seek(0x438)
                _ext4_magic = _rf.read(2)
            if _hdr != b"\x3a\xff\x26\xed" and _ext4_magic != b"\x53\xef":
                _err(
                    f"Rootfs image does not appear to be Android Sparse or ext4 "
                    f"(header: {_hdr.hex()}). Aborting."
                )
                return False
            rootfs_part = f"ROOTFS{slot_idx}"
            if not await self._run_fastboot(
                ["flash", "-S", "24MB", rootfs_part, self.rootfs_path],
                f"Flashing rootfs to '{rootfs_part}' partition",
                total_size=os.path.getsize(self.rootfs_path),
                title=rootfs_part,
            ):
                return False

        if not await self._run_fastboot(
            ["set_active", target_slot],
            f"Setting active slot to '{target_slot.upper()}'",
        ):
            return False

        _section("Rebooting")
        if await self._run_fastboot(["reboot"], "Rebooting device"):
            _ok("Slot-only Fastboot flashing complete!")
        return True

    async def _fastboot_flash_full_image(self) -> bool:
        """Flash a full sparse eMMC image to mmc0. Returns False on failure."""
        if not await self._run_fastboot(
            ["flash", "-S", "24MB", "mmc0", self.emmc_path],
            "Flashing sparse OS image to 'mmc0' root device",
            total_size=os.path.getsize(self.emmc_path),
            title="emmc.img",
        ):
            return False

        if not await self._run_fastboot(
            ["set_active", "a"], "Setting active slot to 'A'"
        ):
            return False

        _section("Rebooting")
        if await self._run_fastboot(["reboot"], "Rebooting device"):
            _ok("Fastboot flashing complete!")
        return True

    async def _state_flash_fastboot(self) -> None:
        _section("Fastboot Flash")

        if self.img_type == "CVI CIMG (Legacy)":
            _err(
                "Warning: Device entered Fastboot mode, but the image is a legacy CIMG format!"
            )
            logger.warning(
                "      Fastboot requires either a raw disk image or an Android Sparse image."
            )

        if not self._resolve_fastboot_bin():
            self.state = "DONE"
            return

        # ── Bootloader flash (mmc0boot0) — skipped by default in slot-only mode ──
        slot_only = bool(self.kernel_path or self.rootfs_path) and not self.emmc_path
        if self.no_bootloader and slot_only:
            logger.info(
                "  [yellow]⚠[/yellow]  Skipping 'mmc0boot0' bootloader flash (use --flash-bootloader to include it)."
            )
        else:
            if not await self._fastboot_flash_bootloader():
                self.state = "DONE"
                return

        if self.bootloader_only:
            _section("Rebooting")
            await self._run_fastboot(["reboot"], "Rebooting device")
            _ok("Bootloader flash complete!")
        elif slot_only:
            await self._fastboot_flash_slot()
        else:
            await self._fastboot_flash_full_image()

        self.state = "DONE"
