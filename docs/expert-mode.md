# Expert Mode Features

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

---
*Footnote: Running the USB flasher inside a virtual machine is highly discouraged and is generally unlikely to work due to how hypervisors interfere with fast-enumerating USB endpoints. If you are attempting this in VMware and experiencing USB enumeration drops during the BootROM handshake, you may need to add quirks to your `.vmx` configuration to skip host resets on the `0x3346:0x1000` device:*

```ini
usb.autoConnect.device0 = "0x3346:0x1000"
usb.quirks.device0 = "0x3346:0x1000 skip-reset, skip-refresh, skip-setconfig"
usb.passthrough.0x3346:0x1000 = "TRUE"
```

## Advanced Usage

The flasher is implemented as a strict, event-driven `asyncio` state machine. Rather than aggressively polling USB endpoints in a blocking loop, the Linux backend dynamically binds to the kernel's `udev` subsystem to listen for hardware hotplug events in real time. This ensures that the Python transport layer can react instantly to physical device enumerations—such as the board transitioning from BootROM to U-Boot—without burning CPU cycles or losing synchronization during rapid hardware hand-offs. (Note: The Windows and macOS backends use polling instead of the event-driven state machine, as they lack the Linux `udev` subsystem).

Under the hood, the flashing process is distinctly multi-stage. It first connects to the SOC's native BootROM using proprietary USB control transfers to upload the `fip.bin` payload directly into DRAM and jump execution to it. Once the newly uploaded U-Boot initializes and renegotiates its USB descriptors natively as an Android Gadget (`0x18d1:0x4ee0`), the state machine transparently shifts protocols, bridging to the standard Android `fastboot` protocol to stream the monolithic sparse OS payload asynchronously over bulk endpoints.

### Running from Source (Developer Mode)
If you are developing the flasher itself from source rather than injecting it via PyPi, you will need to install its dependencies natively using `uv`:

```bash
# Clone the repository and navigate into it, then sync the dependencies:
uv sync

# Execute it directly as a python module
uv run -m smhub_flasher --image-dir /path/to/firmware/
```
