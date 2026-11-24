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
];
function renderNav() {
  const nav = $('main-nav');
  if (!nav) return;
  const current = window.location.pathname.replace(/\/$/, '') || '/';
  nav.innerHTML = NAV_PAGES.map((p) =>
    '<a href="' + p.path + '"' +
    (p.path === current ? ' class="active" aria-current="page"' : '') +
    '>' + p.label + '</a>'
  ).join('') + '<button class="ghost nav-logout" onclick="logout()">Log out</button>';
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
