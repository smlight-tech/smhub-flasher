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

        self.port = serial.Serial(
            port_name, baudrate=115200, timeout=1.0, exclusive=False
        )
        self._auto_login()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _flush_to_ui(self, raw: bytes) -> None:
        if raw:
            try:
                self.on_data_cb(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass

    def _auto_login(self) -> None:
        port = self.port
        old_timeout = port.timeout
        port.timeout = 0.1
        try:
            port.reset_input_buffer()
            port.reset_output_buffer()
            port.write(b"\r")

            start_time = time.time()
            accumulated = b""
            login_detected = False

            while time.time() - start_time < 2.0:
                chunk = port.read(1024)
                if chunk:
                    accumulated += chunk
                    if b"login:" in accumulated:
                        login_detected = True
                        break
                    if b"#" in accumulated or b"$" in accumulated:
                        break
                else:
                    port.write(b"\r")
                    time.sleep(0.1)

            if login_detected:
                port.write(b"smlight\r")
                accumulated = b""
                pwd_detected = False
                start_time = time.time()
                while time.time() - start_time < 1.5:
                    chunk = port.read(1024)
                    if chunk:
                        accumulated += chunk
                        if b"assword:" in accumulated:
                            pwd_detected = True
                            break
                    else:
                        time.sleep(0.05)

                if pwd_detected:
                    port.write(b"smlight\r")
                    prompt = b""
                    start_time = time.time()
                    while time.time() - start_time < 1.5:
                        chunk = port.read(4096)
                        if chunk:
                            prompt += chunk
                            if b"#" in chunk or b"$" in chunk:
                                break
                        else:
                            time.sleep(0.05)
                    self._flush_to_ui(prompt)
                else:
                    self._flush_to_ui(accumulated)
            else:
                self._flush_to_ui(accumulated)
        except Exception:
            pass

        port.timeout = old_timeout

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

        # Hide cursor in the UI terminal during transfer to prevent flicker
        self.on_data_cb("\x1b[?25l")

        import subprocess
        import os
        import sys

        stderr_dest = subprocess.PIPE
        master_fd = None
        slave_fd = None

        try:
            if sys.platform != "win32":
                import pty

                master_fd, slave_fd = pty.openpty()
                stderr_dest = slave_fd
        except Exception:
            master_fd = None
            slave_fd = None

        self._zmodem_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_dest,
            bufsize=0,
            cwd=cwd,
        )

        if slave_fd is not None:
            os.close(slave_fd)
            self._zmodem_proc.stderr = os.fdopen(master_fd, "rb")

        def shuttle_to_port():
            try:
                while getattr(self, "_zmodem_proc", None) is not None:
                    data = self._zmodem_proc.stdout.read(1024)
                    if not data:
                        break
                    self.port.write(data)
            except Exception:
                pass

        def shuttle_stderr_to_ui():
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while getattr(self, "_zmodem_proc", None) is not None:
                    data = self._zmodem_proc.stderr.read(1024)
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        self.on_data_cb(text)
            except OSError:
                # PTY raises OSError (EIO) on Linux when child closes the slave end
                pass
            except Exception:
                pass

        t = threading.Thread(target=shuttle_to_port, daemon=True)
        t.start()

        t2 = threading.Thread(target=shuttle_stderr_to_ui, daemon=True)
        t2.start()

        self._zmodem_proc.wait()
        self._zmodem_proc = None

        # Restore cursor in the UI terminal
        self.on_data_cb("\x1b[?25h")
        return True
