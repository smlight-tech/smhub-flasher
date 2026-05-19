# Advanced & Technical Details

The SMHUB USB Flasher provides several advanced/expert options for development, testing, and slot-based flashing. These are hidden from the standard help output but can be viewed using the `--expert-help` flag.

## Testing Bootloaders (`--test-bootloader`)

Developers modifying U-Boot can use the `--test-bootloader` flag to upload and execute a `fip.bin` directly in DRAM without initiating an eMMC flash. This is extremely useful for rapidly iterating on bootloader changes safely.

```bash
smhub-flasher --test-bootloader --fip /path/to/test_fip.bin
```

## Slot-Based Flashing (`--kernel`, `--rootfs`, `--slot`)

When you do not want to flash an entire `emmc.img` monolith, you can target individual partitions (kernel and rootfs). The flasher supports A/B slotting. By default, it automatically targets the *active* slot (`auto`), but you can explicitly specify the `other` slot or a specific slot (`a` or `b`).

*Note: Even when flashing specific slots, you must still provide the `--fip` argument so the tool can establish the initial connection with the device's BootROM.*

```bash
# Flash kernel and rootfs to the currently active slot
smhub-flasher --fip /path/to/fip.bin --kernel path/to/boot.img --rootfs path/to/rootfs.img

# Flash explicitly to slot B
smhub-flasher --fip /path/to/fip.bin --kernel path/to/boot.img --rootfs path/to/rootfs.img --slot b
```

## Bootloader Flashing (`--flash-bootloader`)

When performing a slot-based flash (using `--kernel` or `--rootfs`), the bootloader (`mmc0boot0`) is skipped by default. You can include it by passing `--flash-bootloader`. Alternatively, you can run a standalone bootloader flash using just `--fip` and `--flash-bootloader`.

```bash
# Flash only the bootloader to mmc0boot0
smhub-flasher --fip /path/to/fip.bin --flash-bootloader
```

## Advanced Usage

The flasher is implemented as a strict, event-driven `asyncio` state machine consisting of several distinct phases (`WAIT_FOR_DEVICE`, `HANDSHAKE_ROM`, `UPLOAD_UBOOT`, `AWAIT_FASTBOOT`, and `FLASH_IMAGE`). The underlying transport layer is highly OS-dependent to maintain synchronization during rapid hardware hand-offs (such as the board transitioning from BootROM to U-Boot). On Linux, the backend dynamically binds to the kernel's `udev` subsystem using `pyudev` to listen for real-time hardware hotplug events, reacting instantly without burning CPU cycles. However, Windows and macOS lack this native subsystem, so their backends are forced to aggressively poll the USB tree instead.

Under the hood, the flashing process is distinctly multi-stage:
1. **BootROM Phase (`0x3346:0x1000`)**: The tool connects to the SOC's native BootROM using proprietary USB control transfers to bypass standard storage enumeration. It uploads the `fip.bin` payload (which contains the Trusted Firmware and U-Boot) directly into the SOC's internal DRAM in chunked blocks, then issues a jump command to execute it.
2. **Re-enumeration Phase**: The SOC resets its USB PHY, dropping off the bus and renegotiating its USB descriptors natively as an Android Gadget (`0x18d1:0x4ee0`). The event loop intercepts this re-enumeration and transitions the state machine automatically.
3. **Fastboot Phase (`0x18d1:0x4ee0`)**: The state machine transparently shifts protocols, bridging to the standard Android `fastboot` protocol. It queries the device's slot status (A/B partitioning) and then streams the monolithic sparse OS payload asynchronously over bulk endpoints directly into the eMMC.

### Running from Source (Developer Mode)
If you are developing the flasher itself from source rather than injecting it via PyPi, you will need to install its dependencies natively using `uv`:

```bash
# Clone the repository and navigate into it, then sync the dependencies:
uv sync

# Execute it directly as a python module
uv run -m smhub_flasher --image-dir /path/to/firmware/
```
