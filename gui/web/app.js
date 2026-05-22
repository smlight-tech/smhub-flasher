// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 SMLIGHT

const $ = (id) => document.getElementById(id);

const screens = {
  driver: $("screen-driver"),
  main: $("screen-main"),
};

// "online" or "local" — determines what Start flashing does
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

function setStatus(text, cls) {
  const pill = $("status-pill");
  pill.textContent = text;
  pill.className = "pill " + cls;
}

function timestamp() {
  const d = new Date();
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return (
    pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) +
    "." + pad(d.getMilliseconds(), 3)
  );
}

function appendLog(line) {
  const log = $("log");
  log.textContent += "[" + timestamp() + "] " + line + "\n";
  log.scrollTop = log.scrollHeight;
}

function replaceLastLog(line) {
  const log = $("log");
  let text = log.textContent;
  if (text.endsWith("\n")) {
    text = text.substring(0, text.length - 1);
  }
  const lastNewlineIdx = text.lastIndexOf("\n");
  if (lastNewlineIdx !== -1) {
    log.textContent = text.substring(0, lastNewlineIdx + 1) + "[" + timestamp() + "] " + line + "\n";
  } else {
    log.textContent = "[" + timestamp() + "] " + line + "\n";
  }
  log.scrollTop = log.scrollHeight;
}

function setPhase(phaseName) {
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
}

function markAllStepsDone() {
  document.querySelectorAll(".step").forEach((el) => {
    el.classList.remove("active");
    el.classList.add("done");
  });
}

function resetSteps() {
  document.querySelectorAll(".step").forEach((s) => s.classList.remove("active", "done"));
  $("progress-bar").style.width = "0%";
  $("progress-bar").classList.remove("success");
  $("progress-text").textContent = "Idle";
  $("timer").textContent = "00:00";
  $("log").textContent = "";
  _emmcImageBytes = 0;
  resetEta();
}

// --- Elapsed-time tracking ---
let _flashStartMs = 0;
let _timerHandle = null;

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}

function startTimer() {
  _flashStartMs = Date.now();
  stopTimer();
  _timerHandle = setInterval(() => {
    $("timer").textContent = formatElapsed(Date.now() - _flashStartMs);

    // ETA: decrement smoothly every tick. Use tqdm's anchored remaining
    // when available; otherwise fall back to the formula-based baseline.
    let remainingMs = -1;
    if (_etaFromTqdm && _etaTqdmAnchorMs > 0) {
      remainingMs = Math.max(0, _etaTqdmRemainingMs - (Date.now() - _etaTqdmAnchorMs));
    } else if (_etaBaselineMs && _etaAnchorMs > 0) {
      remainingMs = Math.max(0, _etaBaselineMs - (Date.now() - _etaAnchorMs));
    }
    if (remainingMs >= 0) {
      $("eta").textContent = formatElapsed(remainingMs) + " left";
    }

    // Progress bar interpolation during eMMC phase.
    // Bursts come in batches every ~20s (fastboot's "Send" then silent
    // "Write" pattern). Move the bar gradually between bursts at the
    // observed average rate, capped a little ahead of the last reported %.
    if (_progressInterpActive && _emmcStartMs > 0 && _emmcEstimatedSec > 0) {
      const elapsedSec = (Date.now() - _emmcStartMs) / 1000;
      // Smooth linear progress over the whole flash duration. The estimate
      // is refined on each real burst (see "progress" handler), so the bar
      // self-corrects every time fastboot reports a percentage.
      const projected = (elapsedSec / _emmcEstimatedSec) * 100;
      // Cap projection at 99 % until fastboot itself reports 100 %; once
      // it does, _progressTargetPct = 100 and the bar reaches 100 % at the
      // same moment tqdm's countdown shows 0:00 (without waiting for the
      // process-exit success event ~1 s later).
      const ceiling = _progressTargetPct >= 100 ? 100 : 99;
      const display = Math.min(ceiling, Math.max(_progressTargetPct, projected));
      $("progress-bar").style.width = display + "%";
      if (_progressLabel && _emmcImageBytes) {
        const displayPct = Math.round(display);
        const bytesNow = Math.round((display / 100) * _emmcImageBytes);
        $("progress-text").textContent =
          `${_progressLabel}: ${humanBytes(bytesNow)} / ${humanBytes(_emmcImageBytes)} (${displayPct}%)`;
      }
    }
  }, 500);
}

function stopTimer() {
  if (_timerHandle) {
    clearInterval(_timerHandle);
    _timerHandle = null;
  }
}

// --- ETA tracking ---
// Formula (based on reference timings): ~17 s fixed + image_size / 13 MB/s.
const FIXED_OVERHEAD_S = 17;
const FLASH_RATE_MBPS = 13;
let _emmcImageBytes = 0;      // known once we have the file or manifest info
let _etaBaselineMs = 0;       // total expected ms from anchor
let _etaAnchorMs = 0;         // Date.now() at the moment we "start counting"
let _etaFromTqdm = false;     // once tqdm gives us an exact remaining
// tqdm-anchored countdown: each progress event with a `remaining` field
// resets these so the timer ticks down smoothly between bursts.
let _etaTqdmAnchorMs = 0;
let _etaTqdmRemainingMs = 0;
// Progress-bar interpolation. Idea: bar position = (elapsed / estimated_total)
// × 100. The estimate is seeded from the image size and refined on every
// real burst by the formula  total = elapsed × 100 / pct. Result: smooth
// linear motion the whole way through, with gentle corrections at each
// fastboot checkpoint.
let _progressTargetPct = 0;       // last % reported by fastboot/tqdm
let _emmcStartMs = 0;             // Date.now() at start of eMMC creep
let _emmcEstimatedSec = 0;        // current best estimate of total flash duration
let _progressLabel = "";          // e.g. "emmc.img" — set from fastboot events
let _progressInterpActive = false; // true while we own progress-text/bar

function computeBaselineEtaMs(imageBytes) {
  if (!imageBytes) return 0;
  const mb = imageBytes / (1024 * 1024);
  return Math.round((FIXED_OVERHEAD_S + mb / FLASH_RATE_MBPS) * 1000);
}

function resetEta() {
  _etaBaselineMs = 0;
  _etaAnchorMs = 0;
  _etaFromTqdm = false;
  _etaTqdmAnchorMs = 0;
  _etaTqdmRemainingMs = 0;
  _progressTargetPct = 0;
  _emmcStartMs = 0;
  _emmcEstimatedSec = 0;
  _progressLabel = "";
  _progressInterpActive = false;
  const el = $("eta");
  el.textContent = "";
  el.classList.add("hidden");
}

function anchorEta() {
  // Called when the device is first detected (transition to Handshake).
  if (!_emmcImageBytes) return;
  _etaBaselineMs = computeBaselineEtaMs(_emmcImageBytes);
  _etaAnchorMs = Date.now();
  _etaFromTqdm = false;
  $("eta").classList.remove("hidden");
}

function startEmmcCreep() {
  // U-Boot just finished — first snap the bar to 100% so the user sees
  // the prior phase complete. Then after the CSS transition settles,
  // reset to 0% (transition briefly disabled to avoid a slow rewind),
  // and seed the creep rate so the bar visibly moves toward fastboot's
  // first reported percentage (typically 18% after ~20 s of silence).
  const bar = $("progress-bar");
  bar.style.width = "100%";
  setTimeout(() => {
    bar.style.transition = "none";
    bar.style.width = "0%";
    // Force reflow so the next width change re-engages the transition.
    void bar.offsetHeight;
    bar.style.transition = "";
    _progressTargetPct = 0;
    _emmcStartMs = Date.now();
    // Seed slightly conservative (~140 % of the formula prediction) so the
    // bar tends to be a touch behind reality at the first burst, and the
    // first correction snaps forward — that reads better than a backward
    // snap. The estimate is recomputed on every burst from
    // total = elapsed × 100 / pct, so a poor seed self-corrects in seconds.
    _emmcEstimatedSec = _emmcImageBytes
      ? Math.max(30, (computeBaselineEtaMs(_emmcImageBytes) / 1000) * 1.4)
      : 110; // fallback
    _progressLabel = _progressLabel || "emmc.img";
    _progressInterpActive = true;
  }, 1600);
}

function parseHHMMSS(s) {
  // "01:28" or "1:23:45"
  const parts = s.split(":").map((x) => parseInt(x, 10));
  if (parts.some(isNaN)) return 0;
  let secs = 0;
  for (const p of parts) secs = secs * 60 + p;
  return secs * 1000;
}

function humanBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

// --- Flasher event handler, called from Python via evaluate_js ---
window.onFlasherEvent = function (evt) {
  switch (evt.type) {
    case "status":
      if (evt.status === "starting" || evt.status === "running") setStatus("Running", "running");
      else if (evt.status === "success") {
        setStatus("Success", "success");
        markAllStepsDone();
        $("progress-bar").style.width = "100%";
        $("progress-bar").classList.add("success");
        $("progress-text").textContent = "Complete. You can unplug the device.";
        $("btn-start").disabled = false;
        $("btn-cancel").disabled = true;
        stopTimer();
        resetEta();
        refreshCacheInfo();
      } else if (evt.status === "cancelled") {
        setStatus("Cancelled", "error");
        $("btn-start").disabled = false;
        $("btn-cancel").disabled = true;
        stopTimer();
        resetEta();
        refreshCacheInfo();
      } else if (evt.status === "error") {
        setStatus("Failed", "error");
        $("progress-text").textContent = "Flashing failed. See log for details.";
        $("btn-start").disabled = false;
        $("btn-cancel").disabled = true;
        stopTimer();
        resetEta();
        refreshCacheInfo();
      }
      break;
    case "phase":
      setPhase(evt.phase);
      // Any phase change disables interpolation; startEmmcCreep below
      // re-enables it for the eMMC Flash phase only.
      _progressInterpActive = false;
      _progressLabel = "";
      if (evt.phase === "BootROM Detection") {
        $("progress-text").innerHTML =
          "➜ <b>Plug in the SMHUB device now</b> " +
          "(or unplug &amp; replug if already connected)";
      } else {
        $("progress-text").textContent = evt.phase;
      }
      // Anchor the ETA countdown the moment the device is first seen.
      if (evt.phase === "BootROM Handshake" && !_etaAnchorMs) {
        anchorEta();
      }
      // When eMMC flashing begins, briefly show the bar at 100% (U-Boot
      // done), reset it to 0%, and start a slow creep so the user sees
      // motion until fastboot's first burst arrives.
      if (evt.phase === "eMMC Flash") {
        startEmmcCreep();
      }
      break;
    case "image_size":
      _emmcImageBytes = evt.bytes;
      break;
    case "progress": {
      const now = Date.now();
      const pct = Number(evt.percent) || 0;
      if (evt.label) _progressLabel = evt.label;
      if (_progressInterpActive && _emmcStartMs > 0) {
        // Refine the duration estimate only on advancing percentages.
        // tqdm redraws the same line many times via \r during fastboot's
        // "Send" phase, so duplicate-pct events are common; recomputing
        // an estimate from a stale pct would freeze the bar.
        if (pct > _progressTargetPct) {
          const elapsedSec = (now - _emmcStartMs) / 1000;
          if (elapsedSec > 1 && pct > 0) {
            // total = elapsed × 100 / pct  (i.e. extrapolate to 100 %).
            // Blend gently with prior estimate to avoid jitter from
            // fastboot's bursty Send/Write cadence.
            const newEstimate = (elapsedSec * 100) / pct;
            _emmcEstimatedSec = 0.3 * _emmcEstimatedSec + 0.7 * newEstimate;
          }
          _progressTargetPct = pct;
        }
        // Bar/text owned by the timer tick — don't snap here.
      } else {
        // Non-eMMC path (U-Boot FIP load) — snap directly.
        _progressTargetPct = pct;
        $("progress-bar").style.width = pct + "%";
        $("progress-text").textContent =
          `${evt.label}: ${evt.current} / ${evt.total} (${pct}%)`;
      }
      // Prefer tqdm's live remaining when it's available; anchor it so
      // the timer-tick can decrement it smoothly between bursts.
      if (evt.remaining) {
        const remMs = parseHHMMSS(evt.remaining);
        if (remMs > 0) {
          _etaFromTqdm = true;
          _etaTqdmAnchorMs = now;
          _etaTqdmRemainingMs = remMs;
          $("eta").classList.remove("hidden");
          $("eta").textContent = formatElapsed(remMs) + " left";
        }
      }
      break;
    }
    case "prep_phase":
      // Download/Verify/Extract phases before flashing begins.
      $("progress-text").textContent = evt.phase;
      // Once we hit "Extracting", the zip is in the cache (just moved).
      // Refresh the UI so user sees the new file immediately.
      if (evt.phase && (evt.phase.startsWith("Extracting") ||
                        evt.phase.startsWith("Using cached"))) {
        refreshCacheInfo();
      }
      break;
    case "download_progress":
      $("progress-bar").style.width = evt.percent + "%";
      $("progress-text").textContent =
        `Downloading: ${humanBytes(evt.downloaded)} / ${humanBytes(evt.total)} (${evt.percent}%)`;
      break;
    case "extract_progress":
      $("progress-bar").style.width = evt.percent + "%";
      $("progress-text").textContent =
        evt.file ? `Extracting ${evt.file}… (${evt.percent}%)` : `Extracting complete`;
      break;
    case "log":
      if (evt.replace) {
        replaceLastLog(evt.line);
      } else {
        appendLog(evt.line);
      }
      break;
    case "ok":
      if (evt.message) appendLog("[OK] " + evt.message);
      break;
    case "fail":
    case "error":
      if (evt.message) appendLog("[ERROR] " + evt.message);
      break;
    case "usb_permission_denied":
      setStatus("Failed", "error");
      $("btn-start").disabled = false;
      $("btn-cancel").disabled = true;
      stopTimer();
      resetEta();
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
  // Fixed display order
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
  // Initial driver-screen vs main-screen routing.
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

  // Release notes modal
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

  // Background click to close modal
  $("notes-modal").addEventListener("click", (e) => {
    if (e.target === $("notes-modal")) {
      $("notes-modal").classList.add("hidden");
    }
  });
  $("btn-refresh-manifest").addEventListener("click", loadManifest);

  // Pre-load manifest (non-blocking)
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
    resetSteps();
    startTimer();
    $("btn-start").disabled = true;
    $("btn-cancel").disabled = false;
    setStatus("Running", "running");
    // Light up the step-1 indicator immediately so the UI feels responsive.
    setPhase("BootROM Detection");
    let res;
    if (currentMode === "online") {
      const channel = $("online-channel").value;
      const version = $("online-version").value;
      if (!version) {
        setStatus("Idle", "idle");
        $("btn-start").disabled = false;
        $("btn-cancel").disabled = true;
        stopTimer();
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
      setStatus("Failed", "error");
      alert("Could not start: " + (res.error || "unknown"));
      $("btn-start").disabled = false;
      $("btn-cancel").disabled = true;
      stopTimer();
    }
  });

  $("btn-cancel").addEventListener("click", async () => {
    await window.pywebview.api.cancel_flash();
    $("btn-cancel").disabled = true;
  });

  const stopPropagation = (e) => e.stopPropagation();

  $("btn-copy-log").addEventListener("click", (e) => {
    stopPropagation(e);
    e.preventDefault();
    copyToClipboard($("log").textContent, $("btn-copy-log"));
  });

  $("btn-clear-log").addEventListener("click", (e) => {
    stopPropagation(e);
    e.preventDefault();
    $("log").textContent = "";
  });

  $("btn-copy-udev").addEventListener("click", (e) => {
    stopPropagation(e);
    e.preventDefault();
    copyToClipboard($("driver-linux-code").textContent, $("btn-copy-udev"));
  });

  switchMode("online");
}

window.addEventListener("pywebviewready", init);
