# SMHUB USB Flasher

A simple, automated tool for flashing and recovering the SMHUB operating system over a direct USB connection.

Whether you are getting a fresh start or recovering a completely unbootable hub, this tool handles the entire process for you automatically.

## 🖥️ 1. Graphical Interface (GUI) - Recommended

For most users, we highly recommend our standalone graphical flasher. It is supported on **Windows, macOS, and Linux**. 

You can download the latest installer directly from the [Releases page](https://github.com/smlight-tech/smhub-flasher/releases). The GUI provides a native, one-click experience without requiring terminal commands, python dependencies, or manual environment setups.

### Key GUI Features
*   **One-Click Flashing:** Automatically downloads, verifies, and flashes the latest stable/beta firmware, or allows you to select a local directory.
*   **Automated Driver Setup:** On **Windows**, it automatically prompts to install and bind the required WinUSB driver under User Account Control (UAC) if missing.
*   **Real-time Output:** Provides live status tracking, progress indicators, and visual logs throughout the flashing cycle.
*   **🔌 USB-to-Serial Recovery Console:** Provides direct command-line access from within the flasher app for recovery and diagnostics if device won't fully boot:
    *   **Auto-Discovery:** Automatically scans COM ports / USB TTY nodes to locate the SMHUB runtime composite serial interface.
    *   **Auto-Login:** Automatically logs into the board shell using default credentials (only works if still using the default password).
    *   **📤 Upload Files:** Push any local file from your computer directly into the active directory of the SMHUB filesystem via ZMODEM.
    *   **📋 Download Logs (One-Click):** Compress all system logs from `/var/log` and automatically transfer them to a folder of your choice on your host.
    *   **💾 Download Backups (One-Click):** Packages active and user configuration and transfers the full backup archive securely back to your PC.

---

## 🛠️ 2. CLI Setup (Cross-Platform)

The Python CLI tool `smhub-flasher` is fully cross-platform and works natively on Linux, macOS, and Windows. If this is your first time using this tool, you need to install its dependencies. On Linux, you also need to grant your terminal the correct permissions to talk to the raw USB ports.

### Install Software 
You will need to install `fastboot` using your system's package manager, and then install the Python flasher using pipx (or pip).

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install fastboot

# Fedora/RHEL
# sudo dnf install android-tools

# macOS
# brew install android-platform-tools


# 2. Install the flasher package
pipx install smhub-flasher
# or
pip install smhub-flasher
```

### Grant USB Permissions 
By default, Linux forbids regular user accounts from modifying low-level USB devices for security. Rather than running the flashing tool as `sudo` (which is dangerous), we can install a permanent "rule" allowing your account safe access.

Simply copy and paste these exact commands into your terminal one-by-one:

```bash
# 1. Download the permission rules directly from the repository
sudo curl -Lo /etc/udev/rules.d/99-smhub-flasher.rules https://raw.githubusercontent.com/smlight-tech/smhub-flasher/main/99-smhub-flasher.rules

# 2. Tell Linux to reload these newly installed rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# 3. Add your current user account to the 'dialout' allowed group
sudo usermod -a -G dialout $USER
```
> **Important:** *If you had to run step #3, you must completely log out of your computer and log back in, or restart your computer, for Linux to recognize your new group permissions!*

---

## ⚡ 3. CLI Usage

Once the setup is complete, you are ready to flash your SMHUB board. The easiest way is to let the tool automatically download the latest official firmware release using the `--online` flag.

### Basic Flashing

Run the flasher tool from your terminal:

```bash
# Automatically download and flash the latest stable release (Recommended)
smhub-flasher --online

# Or, if you have your own local firmware directory (containing fip.bin and emmc.img)
smhub-flasher --image-dir /path/to/my/firmware/

# Or explicitly defining specific local files
smhub-flasher --fastboot --fip /path/to/fip.bin --image /path/to/emmc.img
```

#### The Flashing Process

Once you hit enter, the tool will sit quietly and wait:
1. **Press the Reset button on your SMHUB board** (or plug it in if it's currently disconnected).
2. The terminal will instantly detect it and begin the **ROM Handshake**.
3. You will see the board disconnect and reconnect automatically as it transitions out of hardware-mode into U-Boot. **Do not unplug it!**
4. It will smoothly transition into sending your `emmc.img` payload.
5. Once flashing is complete, the board will reboot automatically and it is safe to unplug.

### Command Line Options

```sh
SMHUB USB Flasher

options:
  -h, --help            show this help message and exit
  --image-dir IMAGE_DIR
                        Path to directory containing fip.bin and emmc.img
  --fip FIP             Explicit path to fip.bin
  --image IMAGE         Explicit path to emmc combined image
  --list                List available online OS images
  --online [ONLINE]     Download and flash an online OS image (default: latest)
  -v, --verbose         increase output verbosity (-v: debug, -vv: packet trace)
  --fastboot            Flash the device using Fastboot mode
  --expert-help         Show help for advanced / expert-only options
```

### Advanced & Technical Details

For developers and power users, the flasher contains several advanced capabilities. See the [Advanced & Technical Details](https://github.com/smlight-tech/smhub-flasher/blob/main/docs/advanced-technical-details.md) page for full details on slot-based flashing, bootloader testing, running from source, and deep-dive architectural documentation on the internal state machine.


## 🔧 Troubleshooting
- **Permission Denied / USBError**: If it immediately crashes claiming it has no access, your Linux user permissions aren't set correctly. Ensure you followed the **First-Time Setup** completely and have logged out/in.
- **I want to see what is failing**: Add the `-v` flag to the end of your command for verbose output (including standard Fastboot activity logs). For deeper debugging, use the `-vv` flag instead, which dumps the raw hexadecimal USB traffic onto the screen so developers can see what the hardware is complaining about at the transport layer.

---

## License

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Copyright &copy; 2026 SMLIGHT

This project is licensed under the [GNU General Public License v3.0](LICENSE). You are free to use, modify, and distribute this software under the terms of that license.
