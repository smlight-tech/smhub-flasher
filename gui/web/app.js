// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 SMLIGHT

const $ = (id) => document.getElementById(id);

const screens = {
  driver: $("screen-driver"),
  main: $("screen-main"),
};

let currentMode = "online";
let currentManifest = null;

function applyDriverScreenForPlatform(platform) {
  const isLinux = platform === "linux";
  $("driver-desc").classList.toggle("hidden", isLinux);
  $("driver-linux-instructions").classList.toggle("hidden", !isLinux);
  if (isLinux) {
    $("driver-title").textContent = "USB permission setup required";
    $("btn-install-driver").textContent = "Refresh";
    $("btn-install-driver").classList.remove("primary");
  } else {
    $("driver-title").textContent = "One-time USB driver setup";
  }
}

function show(screen) {
  Object.values(screens).forEach((s) => s.classList.add("hidden"));
  screens[screen].classList.remove("hidden");
}

// --- GUI state helpers ---
const flashUI = {
  setStatus(text, cls) {
    const pill = $("status-pill");
    pill.textContent = text;
    pill.className = "pill " + cls;
  },

  setPhase(phaseName) {
    const steps = document.querySelectorAll(".step");
    let reached = false;
    steps.forEach((el) => {
      el.classList.remove("active", "done");
      if (el.dataset.phase === phaseName) {
        el.classList.add("active");
        reached = true;
      } else if (!reached) {
        el.classList.add("done");
      }
    });
  },

  markAllStepsDone() {
    document.querySelectorAll(".step").forEach((el) => {
      el.classList.remove("active");
      el.classList.add("done");
    });
  },

  resetSteps() {
    document.querySelectorAll(".step").forEach((s) => s.classList.remove("active", "done"));
    $("progress-bar").style.transform = "scaleX(0)";
    $("progress-bar").classList.remove("success");
    $("progress-text").textContent = "Idle";
    $("timer").textContent = "00:00";
    $("log").textContent = "";
    progress.reset();
  },
};

function timestamp() {
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  const p3 = (n) => String(n).padStart(3, "0");
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}.${p3(d.getMilliseconds())}`;
}

function appendLog(line) {
  const log = $("log");
  log.textContent += "[" + timestamp() + "] " + line + "\n";
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

// --- Elapsed-time + ETA + progress interpolation ---
function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(Math.floor(s / 60)) + ":" + pad(s % 60);
}

function humanBytes(n) {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  n = Number(n || 0);
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(i < 3 ? 1 : 2)) + " " + units[i];
}

function parseHHMMSS(s) {
  const parts = s.split(":").map(Number);
  return parts.some(isNaN) ? 0 : parts.reduce((acc, p) => acc * 60 + p, 0) * 1000;
}

class FlashProgress {
  constructor() {
    this._startMs = 0;
    this._timerHandle = null;
    this._imageBytes = 0;
    this._tqdmPct = 0;
    this._tqdmAnchorMs = 0;
    this._tqdmRemainingMs = 0;
    this._active = false;   // true during eMMC interpolation
    this._label = "";
    this._phaseStartMs = 0; // when eMMC phase began (pre-tqdm fallback)
    this._estMs = 0;        // estimated total duration from image size
  }

  setImageBytes(bytes) {
    this._imageBytes = bytes;
  }

  // Called on each incoming phase event to stop eMMC interpolation.
  deactivate() {
    this._active = false;
    this._label = "";
  }

  startTimer() {
    this._startMs = Date.now();
    this.stopTimer();
    let lastTick = 0;
    const loop = (ts) => {
      if (!this._timerHandle) return;
      if (ts - lastTick >= 500) { lastTick = ts; this._tick(); }
      this._timerHandle = requestAnimationFrame(loop);
    };
    this._timerHandle = requestAnimationFrame(loop);
  }

  stopTimer() {
    if (this._timerHandle) {
      cancelAnimationFrame(this._timerHandle);
      this._timerHandle = null;
    }
  }

  // Resets all ETA/interpolation state including the image size.
  reset() {
    this._tqdmPct = 0;
    this._tqdmAnchorMs = 0;
    this._tqdmRemainingMs = 0;
    this._active = false;
    this._label = "";
    this._phaseStartMs = 0;
    this._estMs = 0;
    this._imageBytes = 0;
    const el = $("eta");
    el.textContent = "";
    el.classList.add("hidden");
  }

  // Snap bar to 100% (U-Boot done), reset to 0%, then start interpolated progress.
  startFlashProgress() {
    const bar = $("progress-bar");
    bar.style.transform = "scaleX(1)";
    setTimeout(() => {
      bar.style.transition = "none";
      bar.style.transform = "scaleX(0)";
      void bar.offsetHeight; // force reflow to re-engage CSS transition
      bar.style.transition = "";
      this._tqdmPct = 0;
      this._tqdmAnchorMs = 0;
      this._label = this._label || "emmc.img";
      this._active = true;
      this._phaseStartMs = Date.now();
      // Estimate total flash time from image size.
      // Measured: ~0.695s send + ~1.38s write per 24 MB chunk = ~10.5 MB/s effective.
      // Add ~3s fixed overhead (bootloader flash + fastboot handshake at phase start).
      this._estMs = this._imageBytes
        ? (3 + (this._imageBytes / (1024 * 1024)) / 10.5) * 1000
        : 110_000;
      $("eta").classList.remove("hidden");
    }, 1600);
  }

  // Handles incoming progress events — updates tqdm anchor or snaps bar directly.
  onProgress(evt) {
    const pct = Number(evt.percent) || 0;
    if (evt.label) this._label = evt.label;
    if (this._active) {
      // Update tqdm anchor; bar/text are driven by the timer tick.
      if (evt.remaining) {
        const remMs = parseHHMMSS(evt.remaining);
        if (remMs > 0) {
          this._tqdmPct = pct;
          this._tqdmAnchorMs = Date.now();
          this._tqdmRemainingMs = remMs;
        }
      }
    } else {
      // Non-eMMC (U-Boot FIP load) — snap directly.
      $("progress-bar").style.transform = `scaleX(${pct / 100})`;
      $("progress-text").textContent =
        `${evt.label}: ${evt.current} / ${evt.total} (${pct}%)`;
    }
  }

  _tick() {
    const now = Date.now();
    $("timer").textContent = formatElapsed(now - this._startMs);

    if (!this._active) {
      if (this._tqdmAnchorMs > 0) {
        const remMs = Math.max(0, this._tqdmRemainingMs - (now - this._tqdmAnchorMs));
        $("eta").textContent = formatElapsed(remMs) + " left";
      }
      return;
    }

    let display;
    if (this._tqdmAnchorMs > 0) {
      const rate = this._tqdmRemainingMs > 0 ? (100 - this._tqdmPct) / this._tqdmRemainingMs : 0;
      display = Math.min(99, this._tqdmPct + rate * (now - this._tqdmAnchorMs));
      const remMs = Math.max(0, this._tqdmRemainingMs - (now - this._tqdmAnchorMs));
      $("eta").textContent = formatElapsed(remMs) + " left";
    } else if (this._estMs > 0) {
      display = Math.min(18, ((now - this._phaseStartMs) / this._estMs) * 100);
    }

    if (display !== undefined) {
      $("progress-bar").style.transform = `scaleX(${display / 100})`;
      if (this._label && this._imageBytes) {
        const pct = Math.round(display);
        const done = humanBytes(Math.round((display / 100) * this._imageBytes));
        const total = humanBytes(this._imageBytes);
        $("progress-text").textContent = `${this._label}: ${done} / ${total} (${pct}%)`;
      }
    }
  }
}

const progress = new FlashProgress();

function setFlashIdle() {
  $("btn-start").disabled = false;
  $("btn-cancel").disabled = true;
  progress.stopTimer();
  progress.reset();
  refreshCacheInfo();
}

// --- Flasher event handler, called from Python via evaluate_js ---
window.onFlasherEvent = function (evt) {
  switch (evt.type) {
    case "status":
      if (evt.status === "starting" || evt.status === "running") flashUI.setStatus("Running", "running");
      else if (evt.status === "success") {
        flashUI.setStatus("Success", "success");
        flashUI.markAllStepsDone();
        $("progress-bar").style.transform = "scaleX(1)";
        $("progress-bar").classList.add("success");
        $("progress-text").textContent = "Complete. You can unplug the device.";
        setFlashIdle();
      } else if (evt.status === "cancelled") {
        flashUI.setStatus("Cancelled", "error");
        setFlashIdle();
      } else if (evt.status === "error") {
        flashUI.setStatus("Failed", "error");
        $("progress-text").textContent = "Flashing failed. See log for details.";
        setFlashIdle();
      }
      break;
    case "phase":
      flashUI.setPhase(evt.phase);
      progress.deactivate();
      $("progress-text").textContent = evt.phase;
      if (evt.phase === "BootROM Detection") {
        $("progress-text").innerHTML =
          "➜ <b>Plug in the SMHUB device now</b> " +
          "(or unplug &amp; replug if already connected)";
      } else if (evt.phase === "eMMC Flash") {
        progress.startFlashProgress();
      }
      break;
    case "image_size":
      progress.setImageBytes(evt.bytes);
      break;
    case "progress":
      progress.onProgress(evt);
      break;
    case "prep_phase":
      // Download/Verify/Extract phases before flashing begins.
      $("progress-text").textContent = evt.phase;

      if (evt.phase && (evt.phase.startsWith("Extracting") ||
                        evt.phase.startsWith("Using cached"))) {
        refreshCacheInfo();
      }
      break;
    case "download_progress":
      $("progress-bar").style.transform = `scaleX(${evt.percent / 100})`;
      $("progress-text").textContent =
        `Downloading: ${humanBytes(evt.downloaded)} / ${humanBytes(evt.total)} (${evt.percent}%)`;
      break;
    case "extract_progress":
      $("progress-bar").style.transform = `scaleX(${evt.percent / 100})`;
      $("progress-text").textContent =
        evt.file ? `Extracting ${evt.file}… (${evt.percent}%)` : `Extracting complete`;
      break;
    case "log":
      appendLog(evt.line);
      break;
    case "ok":
      if (evt.message) appendLog("[OK] " + evt.message);
      break;
    case "fail":
    case "error":
      if (evt.message) appendLog("[ERROR] " + evt.message);
      break;
    case "usb_permission_denied":
      flashUI.setStatus("Failed", "error");
      setFlashIdle();
      applyDriverScreenForPlatform(evt.platform);
      $("driver-status-1").textContent = "Needs setup";
      $("driver-status-1").className = "driver-status err";
      $("btn-driver-continue").disabled = false;
      show("driver");
      break;
  }
};

// --- Mode switching ---
function switchMode(mode) {
  currentMode = mode;
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
  $("panel-online").classList.toggle("hidden", mode !== "online");
  $("panel-local").classList.toggle("hidden", mode !== "local");
  $("panel-console").classList.toggle("hidden", mode !== "console");
  $("panel-flashing-controls").classList.toggle("hidden", mode === "console");

  if (mode === "console") {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.resize_window) {
      window.pywebview.api.resize_window(900, 720);
    }
    initTerminal();
    setTimeout(() => fitAddon.fit(), 0);
  } else {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_log_expanded) {
      const logDetails = document.querySelector(".log-details");
      window.pywebview.api.set_log_expanded(logDetails ? logDetails.open : false);
    }
    // Clean up resource contention if navigating away
    const btn = $("btn-console-connect");
    if (btn && btn.textContent === "Disconnect") {
      btn.click(); // Auto-disconnect
    }
  }

  refreshStartButton();
}

function refreshStartButton() {
  const btn = $("btn-start");
  if (currentMode === "online") {
    const version = $("online-version").value;
    btn.disabled = !version;
    btn.textContent = "Download & flash";
  } else {
    // Local mode — enabled when folder has files
    const status = $("folder-status");
    btn.disabled = !status.classList.contains("ok");
    btn.textContent = "Start flashing";
  }
}

// --- Online: manifest, channels, versions ---
async function refreshCacheInfo() {
  try {
    const info = await window.pywebview.api.get_cache_info();
    const el = $("cache-info");
    if (info.count === 0) {
      el.textContent = "No firmware cached yet.";
    } else {
      el.textContent =
        `${info.count} cached file(s), ${humanBytes(info.total_bytes)} on disk.`;
    }
  } catch (e) { /* ignore */ }
}

async function loadManifest() {
  const statusEl = $("manifest-status");
  statusEl.textContent = "Loading catalog…";
  statusEl.className = "hint";
  try {
    const res = await window.pywebview.api.fetch_manifest(null);
    if (!res.ok) throw new Error(res.error || "fetch failed");
    currentManifest = res.manifest;
    populateChannels(currentManifest);
    statusEl.textContent = `Loaded ${res.manifest.releases.length} releases.`;
    statusEl.className = "hint ok";
  } catch (e) {
    currentManifest = null;
    statusEl.textContent = "Could not load firmware catalog: " + e;
    statusEl.className = "hint err";
    $("online-channel").innerHTML = "";
    $("online-version").innerHTML = "";
    refreshStartButton();
  }
}

function populateChannels(manifest) {
  const releases = manifest.releases || [];
  const channels = {};
  for (const r of releases) {
    const ch = r.channel || "stable";
    if (!channels[ch]) channels[ch] = r.version;
  }
  const sel = $("online-channel");
  sel.innerHTML = "";

  const order = ["stable", "beta", "alpha", "alfa"];
  const seen = new Set();
  const entries = [];
  for (const k of order) {
    if (k in channels) { entries.push([k, channels[k]]); seen.add(k); }
  }
  for (const k of Object.keys(channels)) {
    if (!seen.has(k)) entries.push([k, channels[k]]);
  }
  for (const [name, version] of entries) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name + (version ? " (" + version + ")" : " — unavailable");
    if (!version) opt.disabled = true;
    sel.appendChild(opt);
  }
  // Auto-select first non-disabled channel
  for (const opt of sel.options) {
    if (!opt.disabled) { sel.value = opt.value; break; }
  }
  populateVersions(sel.value);
}

function populateVersions(channel) {
  const sel = $("online-version");
  sel.innerHTML = "";
  if (!currentManifest || !channel) { refreshStartButton(); return; }
  const releases = (currentManifest.releases || [])
    .filter((r) => r.channel === channel);
  for (const r of releases) {
    const opt = document.createElement("option");
    opt.value = r.version;
    opt.textContent = r.version;
    sel.appendChild(opt);
  }
  if (releases.length) sel.value = releases[0].version;
  showReleaseInfo();
  refreshStartButton();
}

function showReleaseInfo() {
  const info = $("online-info");
  const channel = $("online-channel").value;
  const version = $("online-version").value;
  if (!currentManifest || !channel || !version) {
    info.textContent = "";
    return;
  }
  const r = (currentManifest.releases || [])
    .find((x) => x.channel === channel && x.version === version);
  if (!r) { info.textContent = ""; $("btn-release-notes").classList.add("hidden"); return; }
  const art = (r.artifacts && r.artifacts.firmware) || {};
  const parts = [];
  if (r.released_at) parts.push("Released " + r.released_at.slice(0, 10));
  if (art.size_bytes) parts.push("Download " + humanBytes(art.size_bytes));
  if (r.notes) parts.push(r.notes);
  info.textContent = parts.join(" · ");

  if (r.notes_url) {
    $("btn-release-notes").classList.remove("hidden");
    $("btn-release-notes").dataset.url = r.notes_url;
  } else {
    $("btn-release-notes").classList.add("hidden");
  }
}

// --- Driver flow ---
async function refreshDriverStatus() {
  try {
    const dr = await window.pywebview.api.check_driver();
    applyDriverScreenForPlatform(dr.platform);
    if (dr.platform === "linux") {
      $("driver-status-1").textContent = "Installed";
      $("driver-status-1").className = "driver-status ok";
      $("btn-driver-continue").disabled = false;
    } else {
      const s1 = $("driver-status-1");
      s1.textContent = dr.winusb_bound ? "Installed" : "Needs setup";
      s1.className = "driver-status " + (dr.winusb_bound ? "ok" : "err");
      $("btn-install-driver").textContent = dr.winusb_bound ? "Reinstall" : "Install";
      if (dr.winusb_bound) $("btn-install-driver").classList.remove("primary");
      $("btn-driver-continue").disabled = !dr.winusb_bound;
    }
    return dr;
  } catch (e) {
    appendLog("Driver check error: " + e);
    return {};
  }
}

async function copyToClipboard(text, btn) {
  const original = btn.textContent;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    btn.textContent = "Copied!";
  } catch {
    btn.textContent = "Failed";
  }
  setTimeout(() => { btn.textContent = original; }, 1200);
}

async function runInstaller(buttonId, apiCall) {
  const btn = $(buttonId);
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Installing…";
  try {
    const res = await apiCall();
    if (!res.ok) {
      alert("Driver installation failed: " + (res.error || "return code " + res.returncode));
    }
  } catch (e) {
    alert("Error: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    await refreshDriverStatus();
  }
}

// --- UI wiring ---
async function init() {
  let dr;
  try {
    dr = await window.pywebview.api.check_driver();
  } catch (e) {
    dr = {};
    appendLog("Driver check error: " + e);
  }
  // Show the driver screen only on Windows when the WinUSB driver is missing.
  if (dr.platform === "win32" && !dr.winusb_bound) {
    show("driver");
    refreshDriverStatus();
  } else {
    show("main");
  }

  $("btn-install-driver").addEventListener("click", () =>
    runInstaller("btn-install-driver", () => window.pywebview.api.install_driver())
  );
  $("btn-driver-continue").addEventListener("click", () => show("main"));

  // Tabs
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchMode(t.dataset.mode));
  });

  // Online pickers
  $("online-channel").addEventListener("change", () => populateVersions($("online-channel").value));
  $("online-version").addEventListener("change", () => { showReleaseInfo(); refreshStartButton(); });

  $("btn-release-notes").addEventListener("click", async () => {
    const url = $("btn-release-notes").dataset.url;
    if (!url) return;
    
    $("notes-modal").classList.remove("hidden");
    $("notes-body").innerHTML = "Loading notes...";
    
    try {
      const resp = await window.pywebview.api.fetch_notes(url);
      if (!resp.ok) throw new Error(resp.error || "Load failed");
      $("notes-body").innerHTML = marked.parse(resp.text);
    } catch (e) {
      $("notes-body").textContent = "Error loading release notes: " + e.message;
    }
  });

  $("btn-close-notes").addEventListener("click", () => {
    $("notes-modal").classList.add("hidden");
  });

  $("notes-modal").addEventListener("click", (e) => {
    if (e.target === $("notes-modal")) {
      $("notes-modal").classList.add("hidden");
    }
  });
  $("btn-refresh-manifest").addEventListener("click", loadManifest);

  loadManifest();
  refreshCacheInfo();

  $("btn-clear-cache").addEventListener("click", async () => {
    const res = await window.pywebview.api.clear_firmware_cache();
    appendLog(`Cache cleared (${res.removed} file(s))`);
    refreshCacheInfo();
  });

  // Local folder: auto-detect default path
  try {
    const def = await window.pywebview.api.get_default_rom_path();
    if (def.path) {
      $("folder-path").value = def.path;
      if (def.has_files) {
        $("folder-status").textContent = "Auto-detected local firmware ✓";
        $("folder-status").className = "hint ok";
      } else {
        $("folder-status").textContent = "Default folder exists but lacks fip.bin/emmc.img";
        $("folder-status").className = "hint";
      }
    }
  } catch (e) {
    // Ignore; user can Browse manually
  }

  $("btn-pick-folder").addEventListener("click", async () => {
    const folder = await window.pywebview.api.pick_folder();
    if (!folder) return;
    $("folder-path").value = folder;
    const st = await window.pywebview.api.validate_folder(folder);
    const stat = $("folder-status");

    if (st.fip_exists && st.emmc_exists) {
      stat.textContent = "Found fip.bin and emmc.img ✓";
      stat.className = "hint ok";
    } else {
      const missing = [];
      if (!st.fip_exists) missing.push("fip.bin");
      if (!st.emmc_exists) missing.push("emmc.img");
      stat.textContent = "Missing: " + missing.join(", ");
      stat.className = "hint err";
    }

    refreshStartButton();
  });

  $("btn-start").addEventListener("click", async () => {
    flashUI.resetSteps();
    progress.startTimer();
    $("btn-start").disabled = true;
    $("btn-cancel").disabled = false;
    flashUI.setStatus("Running", "running");
    // Light up the step-1 indicator immediately so the UI feels responsive.
    flashUI.setPhase("BootROM Detection");
    let res;
    if (currentMode === "online") {
      const channel = $("online-channel").value;
      const version = $("online-version").value;
      if (!version) {
        flashUI.setStatus("Idle", "idle");
        setFlashIdle();
        alert("Pick a channel and version first.");
        return;
      }
      const force = $("force-redownload").checked;
      res = await window.pywebview.api.download_and_flash(null, channel, version, force);
    } else {
      const folder = $("folder-path").value;
      if (!folder) return;
      res = await window.pywebview.api.start_flash(folder);
    }
    if (!res.ok) {
      flashUI.setStatus("Failed", "error");
      alert("Could not start: " + (res.error || "unknown"));
      setFlashIdle();
    }
  });

  $("btn-cancel").addEventListener("click", async () => {
    await window.pywebview.api.cancel_flash();
    $("btn-cancel").disabled = true;
  });

  $("btn-copy-log").addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    copyToClipboard($("log").textContent, $("btn-copy-log"));
  });

  $("btn-clear-log").addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    $("log").textContent = "";
  });

  $("btn-copy-udev").addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    copyToClipboard($("driver-linux-code").textContent, $("btn-copy-udev"));
  });

  document.querySelector(".log-details").addEventListener("toggle", (e) => {
    if (window.pywebview) window.pywebview.api.set_log_expanded(e.target.open);
  });

  switchMode("online");
}

// --- Recovery Console ---
let terminal = null;
let fitAddon = null;

function initTerminal() {
  if (terminal) return;
  terminal = new window.Terminal({
    cursorBlink: true,
    theme: { background: '#000000' },
    fontFamily: 'monospace',
    fontSize: 12,
    lineHeight: 1.2,
  });
  fitAddon = new window.FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open($("terminal-container"));
  fitAddon.fit();

  terminal.onData((data) => {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.write_console(data);
    }
  });

  window.addEventListener("resize", () => {
    if (currentMode === "console") {
      fitAddon.fit();
    }
  });

  // Enable mouse copy/paste (Linux style)
  terminal.onSelectionChange(() => {
    if (terminal.hasSelection()) {
      navigator.clipboard.writeText(terminal.getSelection()).catch(() => {});
    }
  });

  terminal.element.addEventListener("contextmenu", async (e) => {
    e.preventDefault();
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        window.pywebview.api.write_console(text);
      }
    } catch (err) {
      console.warn("Clipboard paste failed", err);
    }
  });
}

window.writeTerminalData = function(data) {
  if (terminal) terminal.write(data);
};

window.setConsoleStatus = function(status, color) {
  const el = $("console-status");
  el.textContent = status;
  el.style.color = color || "#aaa";
};

$("btn-console-connect").addEventListener("click", async () => {
  const btn = $("btn-console-connect");
  if (btn.textContent === "Connect") {
    btn.disabled = true;
    window.setConsoleStatus("Connecting...", "#aaa");
    try {
      const res = await window.pywebview.api.open_console();
      btn.disabled = false;
      if (res.ok) {
        btn.textContent = "Disconnect";
        window.setConsoleStatus("Connected", "#0f0");
        if (terminal) terminal.focus();
      } else {
        window.setConsoleStatus("Failed: " + res.error, "#f00");
      }
    } catch (e) {
      btn.disabled = false;
      window.setConsoleStatus("Error: " + e, "#f00");
    }
  } else {
    btn.disabled = true;
    try {
      await window.pywebview.api.close_console();
    } catch (e) { /* ignore */ }
    btn.disabled = false;
    btn.textContent = "Connect";
    window.setConsoleStatus("Disconnected", "#aaa");
  }
});

window.addEventListener("pywebviewready", init);
