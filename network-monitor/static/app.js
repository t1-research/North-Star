'use strict';
/* North Star Monitor — shared JS for all dashboard pages.
   Helpers, nav rendering, charts, header/footer status polling, and the
   footer update-check flow. Each page adds only its own small script. */

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const TARGET_COLORS = {
  '1.1.1.1': '#14b8a6', '8.8.8.8': '#38bdf8',
  '192.168.68.1': '#fbbf24', '192.168.1.1': '#a78bfa',
};
const FALLBACK_COLORS = ['#34d399', '#f472b6', '#fb923c', '#94a3b8'];

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}
function relTime(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
  return Math.floor(s / 86400) + 'd ago';
}
function fmtDuration(secs) {
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
  return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
}
function fmtInterval(sec) {
  if (sec == null) return '';
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  return (sec / 3600).toFixed(sec % 3600 === 0 ? 0 : 1) + 'h';
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  // Session expired or signed out elsewhere: back to the login page.
  if (r.status === 401) { window.location.href = '/login'; throw new Error('not authenticated'); }
  if (!r.ok) throw new Error(path + ' -> HTTP ' + r.status);
  return r.json();
}

// ---------- nav bar ----------
// Rendered here so every page gets the same links and the active page is
// always right (derived from the URL, not hardcoded per page).
const NAV_PAGES = [
  { path: '/', label: 'Home' },
  { path: '/devices', label: 'Devices' },
  { path: '/speed-tests', label: 'Speed Tests' },
  { path: '/statistics', label: 'Statistics' },
  { path: '/services', label: 'Services' },
  { path: '/system', label: 'System' },
];
function renderNav() {
  const nav = $('main-nav');
  if (!nav) return;
  const current = window.location.pathname.replace(/\/$/, '') || '/';
  nav.innerHTML = '<span class="nav-links">' + NAV_PAGES.map((p) =>
    '<a href="' + p.path + '"' +
    (p.path === current ? ' class="active" aria-current="page"' : '') +
    '>' + p.label + '</a>'
  ).join('') + '</span><button class="ghost nav-logout" onclick="logout()">Log out</button>';
}

// ---------- charts ----------
// Guarded: only pages that load Chart.js and carry the canvases use
// these. Calling them on a chartless page is a silent no-op.
if (typeof Chart !== 'undefined') {
  Chart.defaults.color = '#8fb0bd';
  Chart.defaults.borderColor = 'rgba(255,255,255,0.07)';
  Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
}

let latencyChart = null;
let speedChart = null;

function timeAxis(unit) {
  return {
    type: 'time',
    time: { unit: unit, tooltipFormat: 'MMM d, h:mm a' },
    ticks: { maxTicksLimit: 8, maxRotation: 0 },
    grid: { color: 'rgba(255,255,255,0.05)' },
  };
}

async function refreshLatencyChart() {
  if (typeof Chart === 'undefined' || !$('latency-chart')) return;
  try {
    const rows = await api('/api/latency?hours=24');
    const byTarget = {};
    rows.forEach((r) => {
      (byTarget[r.target] = byTarget[r.target] || []).push({
        x: r.ts * 1000, y: r.success ? r.rtt_ms : null,
      });
    });
    const targets = Object.keys(byTarget);
    $('latency-empty').style.display = targets.length ? 'none' : 'block';
    if (targets.length) {
      const datasets = targets.map((t, i) => ({
        label: t,
        data: byTarget[t],
        borderColor: TARGET_COLORS[t] || FALLBACK_COLORS[i % FALLBACK_COLORS.length],
        backgroundColor: 'transparent',
        borderWidth: 1.8,
        pointRadius: 0,
        pointHoverRadius: 4,
        spanGaps: false,
        tension: 0.25,
      }));
      if (!latencyChart) {
        latencyChart = new Chart($('latency-chart'), {
          type: 'line',
          data: { datasets },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              x: timeAxis('hour'),
              y: { title: { display: true, text: 'ms' }, beginAtZero: true },
            },
            plugins: { legend: { labels: { boxWidth: 12, usePointStyle: true } } },
          },
        });
      } else {
        latencyChart.data.datasets = datasets;
        latencyChart.update();
      }
    }
  } catch (e) { console.error('latency chart:', e); }
}

async function refreshSpeedChart() {
  // Returns the raw test rows (or null) so the Network page can also
  // render its recent-tests table from the same fetch.
  if (typeof Chart === 'undefined' || !$('speed-chart')) return null;
  try {
    const tests = await api('/api/speedtests?days=7');
    $('speed-empty').style.display = tests.length ? 'none' : 'block';
    if (tests.length) {
      const dl = tests.map((t) => ({ x: t.ts * 1000, y: +t.download_mbps.toFixed(1) }));
      const ul = tests.map((t) => ({ x: t.ts * 1000, y: +t.upload_mbps.toFixed(1) }));
      const datasets = [
        { label: 'Download (Mbps)', data: dl, backgroundColor: '#14b8a6', borderRadius: 3, maxBarThickness: 18 },
        { label: 'Upload (Mbps)', data: ul, backgroundColor: '#f59e0b', borderRadius: 3, maxBarThickness: 18 },
      ];
      if (!speedChart) {
        speedChart = new Chart($('speed-chart'), {
          type: 'bar',
          data: { datasets },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              x: timeAxis('day'),
              y: { title: { display: true, text: 'Mbps' }, beginAtZero: true },
            },
            plugins: { legend: { labels: { boxWidth: 12, usePointStyle: true } } },
          },
        });
      } else {
        speedChart.data.datasets = datasets;
        speedChart.update();
      }
    }
    return tests;
  } catch (e) { console.error('speed chart:', e); return null; }
}

// ---------- footer: last/next update check (launcher only) ----------
// Times come from /api/status on each 30s refresh; a 1s tick keeps the
// "next check in …" countdown live between refreshes.
let updateCheckInfo = null;

function fmtCountdown(secs) {
  if (secs == null) return '';
  if (secs <= 0) return 'any moment';
  if (secs < 60) return 'in ' + secs + 's';
  const m = Math.floor(secs / 60);
  if (m < 60) return 'in ' + m + ' min';
  return 'in ' + Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

function renderUpdateCheckInfo() {
  const el = $('update-check-info');
  if (!el) return;
  if (!updateCheckInfo) { el.style.display = 'none'; return; }
  el.style.display = '';
  const last = updateCheckInfo.last_check_ts
    ? 'Last check: ' + relTime(updateCheckInfo.last_check_ts)
    : 'Last check: —';
  const nextSecs = updateCheckInfo.next_check_ts
    ? updateCheckInfo.next_check_ts - Math.floor(Date.now() / 1000)
    : null;
  el.textContent = ' · ' + last + (nextSecs == null ? '' : ' · Next check ' + fmtCountdown(nextSecs));
}
setInterval(renderUpdateCheckInfo, 1000);

// ---------- shared status refresh (header pills + footer) ----------
// Runs on every page's 30s cycle. Returns {status, services} (either may
// be null on error) so pages can reuse the data instead of refetching:
// Home fills its stat cards + glance strip from it, Services builds
// its grid from it. The two fetches are independent — one failing must
// not kill the other.
async function refreshShared() {
  const out = { status: null, services: null };
  try {
    const st = await api('/api/status');
    out.status = st;

    // header internet pill
    const dot = $('net-dot'), txt = $('net-text');
    if (st.internet_up) {
      dot.className = 'dot ok'; txt.textContent = 'Internet up';
    } else {
      dot.className = 'dot bad'; txt.textContent = 'Internet DOWN';
    }

    // footer version (commit SHA from /api/status, or "dev build" outside a git checkout)
    $('footer-version').textContent = !st.version ? '' : (st.version === 'dev' ? 'dev build' : 'commit ' + st.version);

    // "Check for updates" only exists when a launcher supervises the app.
    $('btn-check-updates').style.display = st.managed ? '' : 'none';
    // Footer update-check times (launcher only; null when standalone).
    updateCheckInfo = st.update_check || null;
    renderUpdateCheckInfo();

    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (e) { console.error('status:', e); }

  try {
    const svcs = await api('/api/services');
    out.services = svcs;
    const down = svcs.filter((s) => s.status === 'down').length;
    const unknown = svcs.filter((s) => s.status === 'unknown').length;
    const pillDot = $('svc-dot'), pillTxt = $('svc-text');
    if (down) {
      pillDot.className = 'dot bad';
      pillTxt.textContent = down + ' service' + (down > 1 ? 's' : '') + ' down';
    } else if (unknown) {
      pillDot.className = 'dot unknown';
      pillTxt.textContent = unknown + ' service' + (unknown > 1 ? 's' : '') + ' unknown';
    } else {
      pillDot.className = 'dot ok';
      pillTxt.textContent = 'All services up';
    }
  } catch (e) { console.error('services:', e); }

  return out;
}

// ---------- Deco restart (System page) ----------
// Mirrors the Deco app's own "Select Deco" reboot screen: a checkbox
// list (name + Main badge + MAC), a Select/Deselect all toggle, and a
// "Restart selected (N)" button with a live count. Only the checked
// units restart. Guarded like the charts: every function no-ops when
// the section's elements aren't on the page (it only exists on /system).
// The unit list comes from /api/status (deco_units + deco_configured),
// fetched by the shared refresh cycle — the System page's inline script
// calls renderDecoUnits with each snapshot.
let decoUnitsKey = null;

function decoCheckedUnits() {
  const out = [];
  document.querySelectorAll('#deco-units .deco-check:checked').forEach((box) => {
    out.push({ name: box.dataset.name, main: box.dataset.main === 'true' });
  });
  return out;
}

function updateDecoRestartButton() {
  const btn = $('btn-deco-restart');
  const toggle = $('deco-toggle-all');
  if (!btn) return;
  const boxes = document.querySelectorAll('#deco-units .deco-check');
  const checked = document.querySelectorAll('#deco-units .deco-check:checked').length;
  btn.textContent = 'Restart selected (' + checked + ')';
  // Disabled while a restart is in flight (btn carries the flag), when
  // nothing is checked, or when the section itself is disabled (no
  // password configured — the boxes are disabled then).
  btn.disabled = btn.dataset.busy === '1' || checked === 0;
  if (toggle) {
    toggle.textContent = boxes.length && checked === boxes.length ? 'Deselect all' : 'Select all';
  }
}

function toggleAllDecoUnits() {
  const boxes = document.querySelectorAll('#deco-units .deco-check:not(:disabled)');
  if (!boxes.length) return;
  const allChecked = document.querySelectorAll('#deco-units .deco-check:checked').length === boxes.length;
  boxes.forEach((box) => { box.checked = !allChecked; });
  updateDecoRestartButton();
}

function renderDecoUnits(st) {
  const list = $('deco-units');
  if (!list) return;
  const units = st.deco_units || [];
  const configured = !!st.deco_configured;
  // The 30s refresh re-renders this list; re-building the DOM would
  // wipe the user's checkbox selections. Only re-render when the list
  // itself (or the configured state) actually changed.
  const key = JSON.stringify(units) + '|' + configured;
  if (key === decoUnitsKey) return;
  decoUnitsKey = key;

  const hint = $('deco-hint');
  if (hint) hint.style.display = configured ? 'none' : '';
  const toggle = $('deco-toggle-all');
  if (toggle) toggle.disabled = !configured || !units.length;

  if (!units.length) {
    list.innerHTML = '<div class="empty">No Deco units configured — add their MACs under deco.units in config.yaml.</div>';
    updateDecoRestartButton();
    return;
  }
  list.innerHTML = units.map((u) =>
    '<label class="deco-row' + (configured ? '' : ' disabled') + '">' +
    '<input type="checkbox" class="deco-check" data-name="' + esc(u.name) + '" data-main="' + (u.main ? 'true' : 'false') + '"' +
    (configured ? '' : ' disabled') + ' onchange="updateDecoRestartButton()">' +
    '<span class="deco-info"><span class="deco-name">' + esc(u.name) +
    (u.main ? ' <span class="deco-badge">Main</span>' : '') + '</span>' +
    '<span class="deco-mac">' + esc(u.mac) + '</span>' +
    (u.main ? '<span class="deco-warn">Restarts the whole network</span>' : '') +
    '</span></label>'
  ).join('');
  updateDecoRestartButton();
}

async function restartSelectedDecos() {
  const btn = $('btn-deco-restart');
  const statusEl = $('deco-status');
  if (!btn) return;
  const selected = decoCheckedUnits();
  if (!selected.length) return;
  const names = selected.map((u) => u.name);
  const mainNames = selected.filter((u) => u.main).map((u) => u.name);
  const msg = 'Restart ' + names.join(', ') + '?\n\n' +
    (mainNames.length
      ? mainNames.join(', ') + (mainNames.length > 1 ? ' are' : ' is') +
        ' the main unit — the whole network drops for ~1–2 min while ' +
        (mainNames.length > 1 ? 'they restart.' : 'it restarts.')
      : 'Only those units restart — the rest of the network stays up.');
  if (!window.confirm(msg)) return;

  btn.dataset.busy = '1';
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = 'Sending…';
  statusEl.textContent = 'Sending restart…';

  let r;
  try {
    r = await fetch('/api/deco/reboot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ units: names }),
    });
  } catch (e) {
    statusEl.textContent = 'Couldn’t reach the server — is the monitor still running?';
    btn.dataset.busy = '';
    btn.textContent = origText;
    updateDecoRestartButton();
    return;
  }
  if (r.status === 401) { window.location.href = '/login'; return; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    statusEl.textContent = 'Restart failed: ' + (data.detail || data.error || ('HTTP ' + r.status));
    btn.dataset.busy = '';
    btn.textContent = origText;
    updateDecoRestartButton();
    return;
  }
  const results = data.results || [];
  const ok = results.filter((x) => x.status === 'rebooting').map((x) => x.name);
  const failed = results.filter((x) => x.status !== 'rebooting');
  let line = ok.length ? ok.join(', ') + ' restarting…' : '';
  if (failed.length) {
    line += (line ? ' ' : '') + failed.map((x) => x.name + ': ' + (x.error || 'failed')).join(' ');
  }
  statusEl.textContent = line;
  if (!ok.length) {
    // Nothing rebooted — restore the button right away.
    btn.dataset.busy = '';
    btn.textContent = origText;
    updateDecoRestartButton();
    return;
  }
  // Wait for the network to settle, then restore the button. If the
  // main unit rebooted, /api/status drops for a minute or two and comes
  // back; if only satellites rebooted, it may never drop at all. So:
  // poll every 5s; once the API answers twice in a row AND at least 10s
  // have passed, we're done. (The 30s page refresh keeps running
  // throughout and updates the header pills on its own.)
  const started = Date.now();
  let everDropped = false;
  let okStreak = 0;
  const deadline = Date.now() + 180000;
  const poll = async () => {
    try {
      await api('/api/status');
      okStreak += 1;
    } catch (e) {
      if (e.message === 'not authenticated') return; // api() already redirected
      everDropped = true;
      okStreak = 0;
    }
    if (okStreak >= 2 && Date.now() - started >= 10000) {
      statusEl.textContent = everDropped
        ? 'Deco is back online.'
        : 'Restart sent — ' + ok.join(', ') + ' rebooting.';
      btn.dataset.busy = '';
      // Clear the selections: the job is done.
      document.querySelectorAll('#deco-units .deco-check').forEach((box) => { box.checked = false; });
      updateDecoRestartButton();
      return;
    }
    if (Date.now() >= deadline) {
      statusEl.textContent = 'Still waiting for the network — this page keeps refreshing on its own.';
      btn.dataset.busy = '';
      updateDecoRestartButton();
      return;
    }
    setTimeout(poll, 5000);
  };
  setTimeout(poll, 5000);
}

// ---------- web console (System page) ----------
// The launcher's console, in the browser: type commands (or use the
// quick buttons) and the app drops trigger files for the launcher —
// the same mechanism as the footer update button. The log pane tails
// data/launcher.log, polled every 2s. Guarded like the Deco section:
// every function no-ops when the section's elements aren't on the page
// (it only exists on /system).
let consolePollTimer = null;
let consoleLogLines = []; // log lines shown so far (echoes not included)
let consoleStopped = false; // quit was sent — the server is gone, stop polling
let consoleRestarting = false; // restart sent — waiting for the server to come back

function consoleAppendLine(text, cls) {
  const pane = $('console-output');
  if (!pane) return;
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  pane.appendChild(div);
  // Keep the last ~200 lines client-side so the DOM stays small.
  while (pane.children.length > 200) pane.removeChild(pane.firstChild);
  pane.scrollTop = pane.scrollHeight;
}

function consoleMergeLogLines(tail) {
  // Append only lines newer than what we already show. The tail is a
  // sliding window, so match the overlap: the longest suffix of what
  // we have that equals a prefix of the tail. (If the launcher trims
  // its log file mid-session the match fails and the tail replays
  // once — cosmetic; the pane caps at 200 lines either way.)
  const maxK = Math.min(consoleLogLines.length, tail.length, 50);
  let k = 0;
  for (let n = maxK; n > 0; n--) {
    let match = true;
    for (let i = 0; i < n; i++) {
      if (consoleLogLines[consoleLogLines.length - n + i] !== tail[i]) { match = false; break; }
    }
    if (match) { k = n; break; }
  }
  tail.slice(k).forEach((line) => {
    consoleLogLines.push(line);
    consoleAppendLine(line);
  });
  if (consoleLogLines.length > 500) consoleLogLines = consoleLogLines.slice(-500);
}

async function consolePollLog() {
  if (consoleStopped || !$('console-output')) return;
  try {
    const data = await api('/api/console/log?lines=100');
    consoleMergeLogLines(data.lines || []);
  } catch (e) {
    // During a restart the server drops for a few seconds — expected,
    // not an error. The restart waiter handles the "back online" line.
    if (e.message === 'not authenticated') return; // api() already redirected
  }
}

function renderConsoleManaged(st) {
  const hint = $('console-unmanaged-hint');
  if (!hint) return;
  hint.style.display = st.managed ? 'none' : '';
}

function initConsole() {
  if (!$('console-output')) return; // not the System page
  consolePollLog();
  consolePollTimer = setInterval(consolePollLog, 2000);
}

function consoleWaitForRestart() {
  // The child restarts in a few seconds; the server drops briefly.
  // Poll /api/status until it answers twice in a row (same pattern as
  // the Deco reboot waiter), then note the recovery in the pane.
  if (consoleRestarting) return;
  consoleRestarting = true;
  consoleAppendLine('Restarting…', 'console-info');
  const deadline = Date.now() + 60000;
  let okStreak = 0;
  const poll = async () => {
    try {
      await api('/api/status');
      okStreak += 1;
    } catch (e) {
      if (e.message === 'not authenticated') { consoleRestarting = false; return; }
      okStreak = 0;
    }
    if (okStreak >= 2) {
      consoleAppendLine('Monitor back online.', 'console-info');
      consoleRestarting = false;
      consolePollLog(); // pick up the restart lines promptly
      return;
    }
    if (Date.now() >= deadline) {
      consoleAppendLine('Still waiting for the monitor — it may have failed to restart. Check the PC console.', 'console-error');
      consoleRestarting = false;
      return;
    }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 2000);
}

async function sendConsoleCommand(cmd) {
  if (!$('console-output')) return;
  cmd = (cmd || '').trim().toLowerCase().split(/\s+/)[0] || '';
  if (!cmd) return;
  if (cmd === 'quit') {
    if (!window.confirm('This stops the monitor completely — restart it on the PC with run_launcher.bat.')) return;
  }
  consoleAppendLine('> ' + cmd, 'console-echo');
  let r;
  try {
    r = await fetch('/api/console/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd }),
    });
  } catch (e) {
    consoleAppendLine('Couldn’t reach the server — is the monitor still running?', 'console-error');
    return;
  }
  if (r.status === 401) { window.location.href = '/login'; return; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  const msg = data.message || data.detail || ('HTTP ' + r.status);
  consoleAppendLine(msg, r.ok ? 'console-info' : 'console-error');
  if (!r.ok) return;
  if (cmd === 'restart') consoleWaitForRestart();
  if (cmd === 'quit') {
    // The server is going away — stop polling instead of spamming
    // failed requests at a dead server.
    consoleStopped = true;
    if (consolePollTimer) clearInterval(consolePollTimer);
    consoleAppendLine('The monitor has stopped. Restart it on the PC with run_launcher.bat.', 'console-info');
  }
}

function sendConsoleCommandFromInput() {
  const input = $('console-input');
  if (!input) return;
  const cmd = input.value;
  input.value = '';
  sendConsoleCommand(cmd);
}

// ---------- actions ----------
async function checkUpdates() {
  const btn = $('btn-check-updates');
  btn.disabled = true;
  btn.textContent = 'Checking…';
  let r;
  try {
    r = await fetch('/api/check-updates', { method: 'POST' });
  } catch (e) {
    btn.textContent = 'Couldn’t reach server';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Check for updates'; }, 4000);
    return;
  }
  if (r.status === 401) { window.location.href = '/login'; return; }
  if (r.status === 409) {
    // No launcher — the button is hidden then, but handle it anyway.
    btn.textContent = 'Updates need the launcher';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Check for updates'; }, 5000);
    return;
  }
  if (!r.ok) {
    btn.textContent = 'Check failed — try again';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Check for updates'; }, 4000);
    return;
  }
  // Poll for a version change. The server goes down briefly while the
  // child restarts, so connection errors just mean "keep waiting".
  const oldVersion = $('footer-version').textContent;
  const deadline = Date.now() + 45000;
  const poll = async () => {
    try {
      const st = await api('/api/status');
      updateCheckInfo = st.update_check || null;
      renderUpdateCheckInfo();
      const v = !st.version ? '' : (st.version === 'dev' ? 'dev build' : 'commit ' + st.version);
      if (v !== oldVersion) {
        btn.textContent = 'Updated — reloading…';
        setTimeout(() => window.location.reload(), 1000);
        return;
      }
    } catch (e) {
      if (e.message === 'not authenticated') return; // api() already redirected
      // Otherwise the server is mid-restart — keep polling.
    }
    if (Date.now() >= deadline) {
      btn.textContent = 'You’re up to date';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Check for updates'; }, 4000);
      return;
    }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 2000);
}

async function logout() {
  try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* server unreachable — still leave */ }
  window.location.href = '/login';
}

// ---------- boot ----------
renderNav();
