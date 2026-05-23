#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown

from . import downloader
from .exceptions import UsbPermissionError
from .flasher_fsm import FlasherFSM
from .monitor import UsbMonitor

console = Console()


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[
        RichHandler(
            console=console,
            markup=True,
            show_time=False,
            show_path=False,
            show_level=False,
        )
    ],
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMHUB Flasher")
    parser.add_argument(
        "--image-dir",
        type=str,
        help="Path to directory containing fip.bin and emmc.img",
    )
    parser.add_argument("--fip", type=str, help="Explicit path to fip.bin")
    parser.add_argument(
        "--image", type=str, help="Explicit path to emmc combined image"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available online OS images",
    )
    parser.add_argument(
        "--online",
        nargs="?",
        const="latest",
        type=str,
        help="Download and flash an online OS image (default: latest)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        help="increase output verbosity (-v: debug, -vv: packet trace)",
        action="count",
        default=0,
    )
    parser.add_argument(
        "--fastboot",
        action="store_true",
        help="Flash the device using Fastboot mode",
    )
    parser.add_argument(
        "--expert-help",
        action="store_true",
        help="Show help for advanced / expert-only options",
    )
    parser.add_argument(
        "--test-bootloader",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--kernel",
        type=str,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--rootfs",
        type=str,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--flash-bootloader",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--slot",
        type=str,
        default="auto",
        choices=["auto", "other", "a", "b"],
        help=(
            "Slot to target for --kernel / --rootfs. "
            "'auto' (default): flash the currently active slot; "
            "'other': flash the inactive slot; "
            "'a'/'b': explicit override."
        ),
    )
    parser.add_argument(
        "--json-fd",
        type=int,
        default=-1,
        help="File descriptor to emit structured JSON events to",
    )
    args = parser.parse_args()

    if args.expert_help:
        expert_help = (
            "Expert options:\n"
            "\n"
            "  --test-bootloader     Load FIP into RAM only, do not burn to eMMC\n"
            "  --kernel PATH         Flash kernel image to boot_<slot>\n"
            "  --rootfs PATH         Flash rootfs image to system_<slot>\n"
            "  --flash-bootloader    Also flash mmc0boot0; standalone with --fip or combined\n"
            "                        with --kernel/--rootfs (default: skip in slot-only mode)\n"
        )
        print(expert_help)
        parser.exit()

    return args


async def async_main() -> None:
    args = parse_args()

    if args.json_fd >= 0:
        from . import events

        events.init_json_pipe(args.json_fd)

    logger.info(f"\n[yellow]{'━' * 64}[/yellow]")
    logger.info("[yellow]  SMHUB USB Flasher[/yellow]")
    logger.info(f"[yellow]{'━' * 64}[/yellow]")
    logger.info("")

    if args.verbose >= 2:
        from .transport import TRACE

        logging.getLogger().setLevel(TRACE)
    elif args.verbose == 1:
        logging.getLogger().setLevel(logging.DEBUG)

    fip_path = args.fip
    emmc_path = args.image
    kernel_path = args.kernel
    rootfs_path = args.rootfs
    slot = args.slot

    if args.image_dir:
        if not fip_path:
            fip_path = os.path.join(args.image_dir, "fip.bin")
        if not emmc_path:
            emmc_path = os.path.join(args.image_dir, "emmc.img")
    fastboot_mode = args.fastboot
    test_bootloader = args.test_bootloader

    # Slot-targeting implies fastboot mode
    slot_only_mode = bool(kernel_path or rootfs_path) and not emmc_path
    if kernel_path or rootfs_path:
        if not fastboot_mode:
            logger.info(
                "  [cyan]ℹ[/cyan]  --kernel/--rootfs implies [cyan]Fastboot[/cyan] mode — enabling automatically."
            )
        fastboot_mode = True

    # --flash-bootloader with only --fip: boot into fastboot, flash mmc0boot0, reboot
    bootloader_only_mode = (
        args.flash_bootloader and not emmc_path and not kernel_path and not rootfs_path
    )
    if bootloader_only_mode:
        if not fastboot_mode:
            logger.info(
                "  [cyan]ℹ[/cyan]  --flash-bootloader implies [cyan]Fastboot[/cyan] mode — enabling automatically."
            )
        fastboot_mode = True

    if fastboot_mode and test_bootloader:
        logger.error("You cannot specify both --fastboot and --test-bootloader!")
        sys.exit(1)

    dl = downloader.CliFirmwareDownloader(logger=logger)

    if args.list:
        try:
            with console.status(
                "Fetching firmware manifest...", spinner="dots", spinner_style="yellow"
            ):
                manifest = dl.fetch_manifest()
            console.print("  [green]✓[/green]  Fetching firmware manifest...")
            console.print("\n[green]Available OS Images:[/green]")
            for release in manifest.get("releases", []):
                version = release.get("version")
                channel = release.get("channel")
                notes = release.get("notes", "No release notes.")
                date_str = release.get("released_at", "")
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        date_str = dt.strftime("%b %d, %Y")
                    except Exception:
                        pass

                console.print(
                    f"[cyan]{version}[/cyan] [ [yellow]{channel}[/yellow] ] - [dim]{date_str}[/dim]",
                    highlight=False,
                )
                console.print(f"  {notes}\n")
        except Exception as e:
            logger.error(f"Failed to fetch images: {e}")
        sys.exit(0)

    fw_version: str | None = None
    fw_channel: str | None = None

    if args.online:
        try:
            channel, version, release = dl.resolve_version(args.online)
            fw_version = version
            fw_channel = channel
            notes_url = release.get("notes_url")
            if notes_url:
                try:
                    import urllib.request

                    with urllib.request.urlopen(notes_url, timeout=5.0) as resp:
                        md_text = resp.read().decode("utf-8")
                        console.print("\n[yellow]  Release Notes[/yellow]")
                        from rich.padding import Padding

                        console.print(Padding(Markdown(md_text), (0, 0, 0, 2)))
                        console.print(f"\n[yellow]{'━' * 64}[/yellow]\n")
                except Exception as e:
                    logger.debug(f"Failed to fetch release notes: {e}")

            console.print("[cyan bold]▶  Firmware Preparation[/cyan bold]")
            img_dir, _, is_fastboot = dl.execute(args.online)
            if is_fastboot:
                fastboot_mode = True
            fip_path = os.path.join(img_dir, "fip.bin")
            emmc_path = os.path.join(img_dir, "emmc.img")
        except Exception as e:
            logger.error(f"Failed to prepare firmware: {e}")
            sys.exit(1)

    if not fip_path:
        logger.error("No FIP specified. Use --fip, --image-dir, or --online.")
        sys.exit(1)
    if (
        not emmc_path
        and not test_bootloader
        and not slot_only_mode
        and not bootloader_only_mode
    ):
        logger.error(
            "No image specified. Use --image / --image-dir for a full flash, "
            "--kernel/--rootfs for slot-only, or --flash-bootloader for bootloader-only."
        )
        sys.exit(1)

    fip_path = os.path.abspath(fip_path)
    emmc_path = os.path.abspath(emmc_path) if emmc_path else ""
    kernel_path = os.path.abspath(kernel_path) if kernel_path else ""
    rootfs_path = os.path.abspath(rootfs_path) if rootfs_path else ""

    if not os.path.exists(fip_path):
        logger.error(f"FIP file not found: {fip_path}")
        sys.exit(1)
    if (
        not test_bootloader
        and not slot_only_mode
        and not bootloader_only_mode
        and not os.path.exists(emmc_path)
    ):
        logger.error(f"EMMC Image not found: {emmc_path}")
        sys.exit(1)
    if kernel_path and not os.path.exists(kernel_path):
        logger.error(f"Kernel image not found: {kernel_path}")
        sys.exit(1)
    if rootfs_path and not os.path.exists(rootfs_path):
        logger.error(f"Rootfs image not found: {rootfs_path}")
        sys.exit(1)

    img_type = "Unknown / Raw"
    if emmc_path and os.path.exists(emmc_path):
        with open(emmc_path, "rb") as f:
            magic = f.read(4)
            if magic == b"CIMG":
                img_type = "CVI CIMG (Legacy)"
            elif magic == b"\x3a\xff\x26\xed":
                img_type = "Android Sparse"

    if fastboot_mode and img_type == "CVI CIMG (Legacy)":
        logger.error(
            "Error: --fastboot selected, but a legacy CIMG image was provided. Fastboot requires Android Sparse or raw images."
        )
        sys.exit(1)

    if not fastboot_mode and img_type == "Android Sparse":
        logger.error(
            "Error: Android Sparse image provided, but --fastboot was not enabled. Legacy CVI flasher requires CIMG or raw images."
        )
        sys.exit(1)

    if slot_only_mode and not fastboot_mode:
        # Should never reach here, but guard anyway
        logger.error("Slot-only mode requires Fastboot.")
        sys.exit(1)

    logger.info("")

    if slot_only_mode:
        logger.info("  Mode:  [cyan]Fastboot — Slot-Only (kernel/rootfs)[/cyan]")
    elif fastboot_mode:
        logger.info("  Mode:  [cyan]Fastboot[/cyan]")
    elif test_bootloader:
        logger.info(
            "  Mode:  [magenta]Test Bootloader (RAM only, no eMMC flash)[/magenta]"
        )
    else:
        logger.info("  Mode:  [magenta]CVI Update (Legacy)[/magenta]")

    if fw_version:
        channel_tag = f" [dim]({fw_channel})[/dim]" if fw_channel else ""
        logger.info(f"  Version: [cyan]{fw_version}[/cyan]{channel_tag}")
    logger.info(f"  FIP:   [white]{fip_path}[/white]")

    if test_bootloader:
        logger.info("  Image: [white]Skipped[/white]")
    elif slot_only_mode:
        slot_label = {
            "auto": "active slot (auto)",
            "other": "inactive slot (auto)",
            "a": "slot A (KERNEL0/ROOTFS0)",
            "b": "slot B (KERNEL1/ROOTFS1)",
        }[slot]
        if kernel_path:
            logger.info(f"  Kernel → {slot_label}: [white]{kernel_path}[/white]")
        if rootfs_path:
            logger.info(f"  Rootfs → {slot_label}: [white]{rootfs_path}[/white]")
    else:
        logger.info(f"  Image: [white]{emmc_path} ({img_type})[/white]")
    logger.info(
        "\n[yellow]  ➜  Please reboot or power-cycle your device now.[/yellow]\n"
    )

    monitor = UsbMonitor(
        target_vids=[0x3346, 0x30B1, 0x18D1],
        target_pids=[0x1000, 0x1001, 0x4EE0],
    )
    monitor.start()

    fsm = FlasherFSM(
        monitor=monitor,
        fip_path=fip_path,
        emmc_path=emmc_path,
        img_type=img_type,
        fastboot_mode=fastboot_mode,
        test_bootloader=test_bootloader,
        kernel_path=kernel_path,
        rootfs_path=rootfs_path,
        slot=slot,
        no_bootloader=not args.flash_bootloader,
        bootloader_only=bootloader_only_mode,
    )

    try:
        await fsm.run()
    except KeyboardInterrupt:
        logger.info("Cancelled by user")
    finally:
        monitor.stop()
        dl.cleanup()


def get_bundled_libusb_backend() -> Any:
    """Get PyUSB libusb1 backend with PyInstaller _MEIPASS support for macOS."""
    if sys.platform == "linux":
        return None
    try:
        import libusb_package
        import usb.backend.libusb1

        def _mac_safe_find_library(candidate: str) -> str | None:
            if getattr(sys, "frozen", False):
                meipass = getattr(sys, "_MEIPASS", "")
                ext = (
                    ".dylib"
                    if sys.platform == "darwin"
                    else ".so"
                    if sys.platform == "linux"
                    else ".dll"
                )
                lib_name = f"libusb-1.0{ext}"

                # Search MEIPASS, and for macOS .app bundles, the Frameworks directory
                search_dirs = [meipass]
                if sys.platform == "darwin":
                    search_dirs.append(os.path.join(meipass, "..", "Frameworks"))

                for base_dir in search_dirs:
                    for sub_dir in ["", "libusb_package"]:
                        full_path = os.path.join(base_dir, sub_dir, lib_name)
                        if os.path.exists(full_path):
                            return os.path.abspath(full_path)

            return libusb_package.find_library(candidate)

        return usb.backend.libusb1.get_backend(find_library=_mac_safe_find_library)
    except ImportError:
        return None


def main() -> None:
    try:
        import usb.core

        _backend = get_bundled_libusb_backend()
        if _backend is not None:
            _orig_find = usb.core.find

            from typing import Any

            def _patched_find(*args: Any, **kwargs: Any) -> Any:
                if "backend" not in kwargs:
                    kwargs["backend"] = _backend
                return _orig_find(*args, **kwargs)

            usb.core.find = _patched_find

        asyncio.run(async_main())
    except KeyboardInterrupt:
        console.print("\n[yellow]  ⚠  Flasher Cancelled by User[/yellow]\n")
        sys.exit(130)
    except UsbPermissionError:
        try:
            from . import events

            events.emit(type="usb_permission_denied")
        except Exception:
            pass
        console.print("\n[red]  ✗  USB Access Denied[/red]\n")
        if sys.platform == "linux":
            console.print(
                "[yellow]Run the following in a terminal to fix this:[/yellow]\n"
            )
            console.print(
                "[white]  cat << 'EOF' | sudo tee /etc/udev/rules.d/99-smhub-flasher.rules\n"
                '  SUBSYSTEM=="usb", ATTR{idVendor}=="3346", ATTR{idProduct}=="1000", MODE="0666"\n'
                '  SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", ATTR{idProduct}=="4ee0", MODE="0666"\n'
                "  EOF\n"
                "  sudo udevadm control --reload-rules\n"
                "  sudo udevadm trigger[/white]\n"
            )
            console.print("[yellow]Then reconnect the device and retry.[/yellow]\n")
        elif sys.platform == "darwin":
            console.print(
                "[yellow]macOS blocked access to the USB device.\n"
                "Try running with [white]sudo[/white], or check System Settings → Privacy & Security.[/yellow]\n"
            )
        else:
            console.print(
                "[yellow]The OS denied access to the USB device. Try running as Administrator.[/yellow]\n"
            )
        sys.exit(1)
    except usb.core.USBError as e:
        console.print(f"\n[red]  ✗  FATAL USB Error: {e}[/red]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
