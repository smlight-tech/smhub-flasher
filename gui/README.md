# SMHUB Flasher — GUI

A cross-platform desktop GUI wrapper around `smhub_flasher` for Windows, macOS, and Linux users. It provides a simple, modern interface to recover firmware on SMLIGHT SMHUB devices over USB.

This folder is **self-contained** and does not modify any file in the original
`smhub_flasher/` package. It spawns the CLI as a subprocess and parses its
output to drive the GUI.

## Architecture

```text
app.py                → pywebview window + JS API bridge
flasher_runner.py     → spawns `python -m smhub_flasher`, parses stdout
driver_check.py       → detects OS permissions (WinUSB on Windows, udev on Linux)
web/                  → HTML/CSS/JS frontend rendered by native WebViews
build-*.sh/ps1        → Platform-specific PyInstaller build scripts
flatpak/              → Architecture and manifest for the official Linux Flatpak
```

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

**Windows:**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
cd gui
uv sync
uv run python app.py
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd gui
uv sync
uv run python app.py
```

## Building Native Packages

We provide build scripts for all major platforms. You must download the required dependencies first:

### Windows (.exe)

Before building, you need to compile the silent USB driver installer (`smhub-simple.exe`):

1. Run the libwdi build script:
   ```powershell
   .\build-wdi.ps1
   ```
2. Build the Flasher:
   ```powershell
   .\build.ps1
   ```
   Output: `dist\SMHUB-Flasher.exe`

### macOS (.dmg)

```bash
./build-mac.sh
```
Output: `dist/SMHUB-Flasher-Mac.dmg`

### Linux (Flatpak & Executable)

We recommend Flatpak for Linux distribution.

```bash
cd ../
./flatpak/build-flatpak.sh
```
Output: `SMHUB-Flasher.flatpak` in the repository root.

You can also build a raw executable using `./build-linux.sh` for testing.

## User Flow

1. User downloads and launches the flasher application.
2. **Platform Permissions:** 
   - **Windows:** If WinUSB isn't yet bound, the GUI prompts to install the driver via `smhub-simple.exe` under UAC.
   - **Linux:** If udev rules are missing, the GUI provides a terminal command to apply `99-smhub-flasher.rules`.
3. User picks the firmware folder (containing `fip.bin` and `emmc.img`) and clicks **Start flashing**.
4. User plugs in the SMHUB board — the GUI detects it, runs the handshake, and streams the eMMC image with a live progress bar.
5. Done screen appears when the board reboots.

## Known limitations

- **No code signing.** On first download on Windows/macOS, security mechanisms (SmartScreen/Gatekeeper) will warn about an "Unknown publisher." A code-signing certificate would eliminate this.

## License

GPLv3 — see top-level [LICENSE](../LICENSE).
