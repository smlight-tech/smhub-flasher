import threading
import time
import codecs
import json

import serial
import serial.tools.list_ports


class SerialConsole:
    # Exclude BootROM and Fastboot VID:PIDs so we don't intercept the flasher
    EXCLUDED_VID_PIDS = [(0x3346, 0x1000), (0x18D1, 0x4EE0)]

    def __init__(self, on_data_cb, on_disconnect_cb):
        self.port = None
        self._thread = None
        self._stop_event = threading.Event()
        self._zmodem_proc = None
        self.on_data_cb = on_data_cb
        self.on_disconnect_cb = on_disconnect_cb

    def find_port(self) -> str | None:
        comports = serial.tools.list_ports.comports()

        # 1. Look for standard Linux composite gadget (SMHUB runtime mode)
        for p in comports:
            if p.vid == 0x1D6B:
                return p.device

        # 2. Fallback to any port that isn't explicitly excluded
        for p in comports:
            if p.vid and p.pid and (p.vid, p.pid) not in self.EXCLUDED_VID_PIDS:
                return p.device

        return None

    def connect(self) -> None:
        port_name = self.find_port()
        if not port_name:
            raise RuntimeError(
                "Could not automatically discover SMHUB serial port. Make sure the device is fully booted."
            )

        self.port = serial.Serial(port_name, baudrate=115200, timeout=1.0)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop_event.set()
        if self.port:
            try:
                # Send Ctrl+D (EOF) to log out of the shell cleanly
                self.port.write(b"\x04")
                # Drop DTR to signal a hangup (SIGHUP) to the remote TTY
                self.port.dtr = False
                self.port.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self.port = None

    def write(self, data: str) -> None:
        if self.port and self.port.is_open:
            # Depending on the shell, \r is usually sufficient.
            # We'll test if we need \r\n replacement later.
            try:
                self.port.write(data.encode("utf-8"))
            except Exception:
                pass

    def _read_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not self._stop_event.is_set():
            if self.port and self.port.is_open:
                try:
                    to_read = max(1, min(4096, getattr(self.port, "in_waiting", 1)))
                    raw = self.port.read(to_read)
                    if raw:
                        if getattr(self, "_zmodem_proc", None):
                            self._zmodem_proc.stdin.write(raw)
                            self._zmodem_proc.stdin.flush()
                        else:
                            text = decoder.decode(raw)
                            if text:
                                self.on_data_cb(text)
                except Exception:
                    self.disconnect()
                    self.on_disconnect_cb()
                    break
            else:
                time.sleep(0.1)

    def run_zmodem(self, cmd: list, cwd: str = None) -> bool:
        """Runs a local zmodem command (sz/rz) attached to the serial port."""
        if not self.port or not self.port.is_open:
            return False

        import subprocess

        self._zmodem_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=cwd,
        )

        def shuttle_to_port():
            try:
                while True:
                    data = self._zmodem_proc.stdout.read(1024)
                    if not data:
                        break
                    self.port.write(data)
            except Exception:
                pass

        t = threading.Thread(target=shuttle_to_port, daemon=True)
        t.start()

        self._zmodem_proc.wait()
        self._zmodem_proc = None
        return True
