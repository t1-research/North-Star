# North Star Monitor

A home network monitoring dashboard for the Deco mesh at 192.168.68.0/24 —
device tracking, latency graphs, speed test history, outage logging, a down
detector for the services the household uses, and Wake-on-LAN, all on one
web page. Phase 1 of the "Deco app on steroids" plan.

**LAN-only by design.** There is no login and no encryption. This is safe on
your home network. **Never port-forward it to the internet.** To reach it
remotely, use Tailscale (your PC's 100.x address, same port 8080).

## What it does

| Feature | How it works |
|---|---|
| Device scan (every 2 min) | Ping-sweeps 192.168.68.0/24, resolves MACs from the ARP table, auto-identifies each device's name |
| Auto-identification | Devices name themselves from mDNS, NetBIOS, reverse DNS, or their hardware vendor — see below |
| Latency probe (every 1 min) | Pings 1.1.1.1, 8.8.8.8, the Deco (192.168.68.1) and the CAX30 (192.168.1.1) |
| Speed test (hourly + on demand) | speedtest-cli, results stored and charted |
| Outage detection | Opens an outage event when ALL external targets fail in one probe round; closes when any succeeds |
| Down detector (every 2 min) | HTTP health checks for the services in config.yaml; down after 2 consecutive failures, recovery on the first success |
| Wake-on-LAN | Magic packet buttons for devices with `wol: true` in config.yaml |
| Auto-updates (launcher) | `launcher.py` polls GitHub, pulls new commits, and restarts the monitor — see below |
| Dashboard | Status cards, 24h latency chart, 7d speed chart, device table, service status grid, event log |

Data lives in `data/monitor.db` (SQLite). Delete it any time to start fresh.

## Automatic device identification

Devices already broadcast identifying info on the LAN, so the monitor
harvests it instead of making you name every device by hand. Each scan,
devices with no manual name are identified in this priority order:

1. **mDNS / Bonjour** — Apple devices, printers, and some TVs/IoT gear
   broadcast a friendly name (e.g. "Brayden's iPhone"). One shared browse
   per scan round covers the whole LAN (via the `zeroconf` package — if
   it's missing, mDNS is skipped and the rest still works).
2. **NetBIOS** — Windows PCs answer an NBSTAT query on UDP 137 with their
   computer name.
3. **Reverse DNS** — a PTR lookup against the system resolver (works when
   the local DNS answers for LAN hosts, e.g. the Deco's DHCP names).
4. **OUI vendor** — the MAC prefix maps to a manufacturer in the offline
   `app/oui.json` table (built from the IEEE list), e.g. "Apple device".
5. **Unknown** — nothing answered; the device still shows with its MAC.

A name in config.yaml **always wins** and is never overwritten — manually
named devices aren't even probed. Discovered names are cached in memory
(re-probed at most once an hour; still-unnamed devices are re-probed on
every scan, since a sleeping phone may answer next time). The dashboard
shows the effective name plus a small source tag on auto-discovered names
(mDNS / NetBIOS / DNS / Vendor); manual names show no tag. The API
(`GET /api/devices`) returns `discovered_name` and `name_source` per
device alongside the manual `name`.

## Down detector

The `services` list in config.yaml names the websites and services the
household actually uses (your own sites plus Google, GitHub, Steam, Discord,
YouTube, Cloudflare). Every `service_check_interval_seconds` (default 120)
the monitor GETs each URL with a 10-second timeout (stdlib urllib only —
no extra dependency). A 2xx/3xx final status is up; 4xx/5xx, a timeout, or a
DNS/connection failure is down.

- **2-failure rule:** a service is only declared down after 2 consecutive
  failed checks, so a single blip never raises an alarm. It recovers on the
  first success after that. Down windows appear in the event log with their
  duration, and a service that stays down gets exactly one open event.
- **Internet vs service:** if the internet itself is down (all external
  latency targets failing), service checks are skipped and every service
  shows as "unknown" — you get one internet outage, not eight false service
  outages. Checks skipped this way don't count against a service's 24h
  uptime %.
- **Dashboard:** the Services section shows a card per service — status dot
  (green up / red down / grey unknown), last response time, and 24h uptime.
  The header pill turns red while any service is down.
- **API:** `GET /api/services` returns the current status per service;
  `GET /api/services/history?hours=24` returns the raw check rows.

## Adding alerts (Discord / email later)

Alerting is dashboard-only for now, but the hook is already in place.
`app/notifiers.py` has a tiny registry: `notify(event)` fans an event dict
out to every registered notifier, and the collectors call it on service
down/recovery and outage open/close. To add a Discord webhook notifier:

1. In Discord: Server Settings → Integrations → Webhooks → New Webhook,
   copy the URL.
2. Add `discord_webhook_url: "https://discord.com/api/webhooks/…"` to
   config.yaml (and a matching default in `app/config.py`).
3. In `app/notifiers.py`, write a function that POSTs a JSON
   `{"content": ...}` message to that URL with stdlib urllib, and
   `register()` it. Keep it in a `try/except` — `notify()` already guards
   each notifier, but a notifier should never raise on a bad response.

An email notifier works the same way: a function taking the event dict,
sending via `smtplib` with credentials from config (a Gmail app password,
not your real password).

## Launcher & auto-updates

The launcher works like the Minecraft launcher: it checks for updates, runs
the application, restarts it when a new version lands, and reports the
current version. Start it once and you never manually stop or restart the
monitor again.

- **Start it:** double-click `run_launcher.bat` on Windows
  (`run_launcher.sh` on Linux/macOS, or `python launcher.py`). It creates
  the venv and installs dependencies on first run, then starts the monitor
  as a child process and supervises it. `run.bat` / `run.py` still work as
  the direct-launch path — same monitor, no updater.
- **How updates flow:** every `update_check_interval_seconds` (default
  300) the launcher asks GitHub for the latest commit on the configured
  branch (`main` by default). When a push lands, it pulls and restarts the
  monitor. If `launcher.py` itself changed, the launcher restarts itself
  too, so nothing ever goes stale. The dashboard footer and
  `GET /api/status` show the commit the running monitor was built from.
- **Crashes:** if the monitor exits on its own, the launcher restarts it
  with backoff (5s, 15s, 30s cap). If it fails to stay up 3 times in a row
  after an update, the launcher logs a clear warning that the new version
  may be broken and keeps retrying (no auto-rollback in v1).
- **⚠️ The one rule: edit `config.local.yaml`, never `config.yaml`.**
  The launcher pulls with `git pull --ff-only`, and any edits to tracked
  files will make every pull fail — updates just stop. Copy
  `config.local.yaml.example` to `config.local.yaml` and put your settings
  there (device names, intervals, port). It deep-merges over config.yaml
  (nested settings merge; lists and single values replace — except
  `devices`, which merges entry by entry, so you only list the devices you
  want to change). `config.local.yaml` is untracked and ignored by git.
- **Disable auto-updates:** set `auto_update: false` in
  `config.local.yaml`. The launcher still supervises the monitor, it just
  stops checking for new commits.
- **No git, no problem:** if git isn't installed or the folder isn't a git
  checkout, the launcher logs a warning once and just runs the monitor
  without update checks.

## Setup (gaming PC, Windows 11)

1. **Install Python 3.11+** from python.org. On the first installer screen,
   tick **"Add python.exe to PATH"**.
2. Copy this folder somewhere permanent, e.g. `C:\Tools\network-monitor`.
3. Open a terminal in the folder and install the dependencies (a venv is
   recommended so it doesn't touch your system Python):
   ```bat
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. **Name your devices** (optional): most devices name themselves — see
   "Automatic device identification" above. To override a name, copy
   `config.local.yaml.example` to `config.local.yaml` and add the device
   there with a `name` (matched by MAC first, so it survives DHCP changes,
   then by IP) and restart the server. Add `wol: true` to a device entry
   for a Wake-on-LAN button.
5. **Run it:** double-click `run_launcher.bat` (recommended — auto-updates
   included), then open http://localhost:8080
   (or http://192.168.68.68:8080 from any device on the LAN). `run.bat`
   starts the monitor directly, without the updater.

## Start on boot (Task Scheduler)

1. Open Task Scheduler → Create Task.
2. General: name it "North Star Monitor", tick **"Run whether user is
   logged on or not"** and **"Run with highest privileges"** is NOT needed.
3. Triggers: **At startup**.
4. Actions: Start a program → `C:\Tools\network-monitor\run_launcher.bat`,
   Start in: `C:\Tools\network-monitor`.
5. Settings: untick "Stop the task if it runs longer than…".

## Notes & limits

- **Devices that block ping won't appear.** Some phones/IoT gear ignore ICMP.
  They may still show up if they have an ARP entry, but ping is the main
  discovery method in v1.
- **Per-device bandwidth is not available in v1.** The Deco doesn't expose
  it outside its app. The CAX30 traffic meter and Deco local API are Phase 2
  experiments.
- **The CAX30 (192.168.1.1) has no MAC in the device table.** It sits on
  another subnet, so it never appears in this PC's ARP table. It is still
  pinged every minute and charted as a latency target.
- **WoL needs the target's MAC.** For devices listed by IP only, the MAC is
  learned from the first ARP scan — the Wake button appears after that.
  WoL also needs to be enabled in the target's BIOS and network adapter.
- Speed tests use ~100–500 MB of traffic each (they saturate the connection
  briefly). Hourly is the default; raise `speedtest_interval_seconds` in
  config.local.yaml if that bothers you.

## Project layout

```
run.py / run.bat / run.sh   entry points, direct launch (Windows / Linux)
launcher.py                 auto-updating launcher (supervises run.py)
run_launcher.bat / .sh      entry points for the launcher (Windows / Linux)
config.yaml                 all settings + known devices + watched services (tracked — don't edit on your PC)
config.local.yaml.example   template for per-machine overrides (copy to config.local.yaml, untracked)
requirements.txt
app/
  main.py       FastAPI app + API endpoints + dashboard route
  collectors.py background jobs (scan / latency / speedtest / services / outages)
  services.py   down detector: HTTP health checks + service status logic
  notifiers.py  pluggable alert notifiers (dashboard-only log notifier for now)
  identify.py   auto device identification (mDNS / NetBIOS / rDNS / OUI)
  oui.json      offline MAC-prefix -> vendor table (built from the IEEE list)
  network.py    cross-platform ping sweep + ARP parsing + MAC utils
  db.py         SQLite schema + queries
  wol.py        Wake-on-LAN magic packet
  config.py     config loading
static/
  index.html    the dashboard (vanilla JS + Chart.js via CDN)
tests/
  smoke_test.py CI smoke test: boots the server, hits every endpoint
data/
  monitor.db    created on first run
```

## API

| Endpoint | What it does |
|---|---|
| `GET /` | Dashboard |
| `GET /api/status` | Snapshot: internet up/down, latest latency, last speed test, device counts, running version (commit SHA) + started_at |
| `GET /api/devices` | All known devices with online/last-seen, manual name, and discovered name + source |
| `GET /api/latency?hours=24` | Latency history |
| `GET /api/speedtests?days=7` | Speed test history |
| `GET /api/events` | Outage/event log, newest first |
| `GET /api/services` | Current status per service (up/down/unknown, last response, 24h uptime %) |
| `GET /api/services/history?hours=24` | Per-service check history |
| `POST /api/speedtest/run` | Start a speed test in the background (202, or 409 if one is running) |
| `POST /api/wol/{mac}` | Send a WoL magic packet |

## Phase 2 ideas (not built yet)

- Deco local/cloud API experiments (reboot, block/unblock devices, per-device
  bandwidth) — reverse-engineered, may break on firmware updates.
- DNS server mode (Pi-hole-style ad blocking + per-device DNS logs).
- Alerting: the notifier interface is in place (`app/notifiers.py`); the
  next step is a Discord webhook and/or email notifier implementation.
