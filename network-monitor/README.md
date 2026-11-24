# North Star Monitor

A self-hosted home network monitoring dashboard: live device tracking,
latency graphs, speed test history, outage detection, a down detector for
the services the household uses, Wake-on-LAN, and per-unit restarts of a
TP-Link Deco mesh — on six dashboard pages. It started as a "Deco app on
steroids" project: everything the Deco app shows about the network, plus
the history, the charts, and the controls it doesn't have.

**LAN-only by design.** The dashboard is plain HTTP with no encryption,
and it is meant to be reached only from your own network. A login password
is optional but recommended — set one with `python set_password.py`
(see [Login](#login)). **Never port-forward it to the internet.** To
reach it remotely, use Tailscale (your PC's 100.x address, same port
8080).

Current state: repo v0.7.0, dashboard v1.9.

## What it is

| Feature | How it works |
|---|---|
| Device scan (every 2 min) | Ping-sweeps the configured subnet, resolves MACs from the ARP table, auto-identifies each device's name |
| Auto-identification | Devices name themselves from mDNS, NetBIOS, reverse DNS, or their hardware vendor — see [below](#automatic-device-identification) |
| Latency probe (every 1 min) | Pings 1.1.1.1, 8.8.8.8, the Deco gateway, and the modem/router |
| Speed test (every 15 min + on demand) | speedtest-cli, results stored and charted |
| Outage detection | Opens an outage event when ALL external targets fail in one probe round; closes when any succeeds |
| Down detector | HEAD health checks for the services in config.yaml (per-service intervals; Vercel sites once a day); a service is down only after 2 consecutive failures, and recovers on the first success. GitHub, Discord, and Cloudflare cards also show the vendor's own reported status from its official status feed |
| Wake-on-LAN | Magic packet buttons for devices with `wol: true` in config.yaml |
| Deco restart | System page: pick any combination of Deco units and restart just those — the rest of the mesh stays up (see [Restart Deco](#restart-deco-system-page)) |
| Login (optional) | Password set with `python set_password.py`; session cookie lasts 7 days; 5 failed logins lock an IP out for 5 minutes |
| Auto-updates (launcher) | `launcher.py` polls GitHub, pulls new commits, and restarts the monitor — see [Launcher & auto-updates](#launcher--auto-updates). The dashboard footer has a Check for updates button (launcher only) to run a check immediately, and shows when the last check ran and when the next one is due |

### The six pages

All pages share one stylesheet and script (`static/style.css`,
`static/app.js`) and refresh every 30 seconds.

| Page | Route | Contents |
|---|---|---|
| Home | `/` | Status cards (internet, latency, last speed test, devices online), latest speed test, services at a glance |
| Devices | `/devices` | Device table with Connected / Disconnected / All filter tabs; Sweep offline button removes stale offline rows (config-defined devices are never swept) |
| Speed Tests | `/speed-tests` | 7-day speed chart, on-demand test button, recent-tests table |
| Statistics | `/statistics` | 24h latency chart, per-service uptime table |
| Services | `/services` | Down detector grid — one card per watched service |
| System | `/system` | Network controls — currently the Deco restart |

## Architecture

- **Stack:** Python 3.12, FastAPI + uvicorn, SQLite (WAL mode), vanilla
  JavaScript + Chart.js (CDN) on the frontend. No build step, no frontend
  framework.
- **Background collectors** (`app/collectors.py`) run on threads inside
  the server process: device scan, latency probe, speed test, service
  health checks, outage detection. They write to SQLite; the API reads
  from it.
- **Launcher** (`launcher.py`, standard library only) supervises the
  monitor: it makes sure the venv and dependencies exist, starts `run.py`
  as a child process, restarts it on crashes (with backoff), and polls
  GitHub for updates — on a new commit it pulls and restarts the child.
  If `launcher.py` itself changed, the launcher restarts itself too.
- **Data** lives in `data/monitor.db` (SQLite, gitignored). Delete it any
  time to start fresh. `data/update_status.json` records the launcher's
  last/next update check for the dashboard footer.
- **Secrets** live in `config.local.yaml` (gitignored): the dashboard
  password hash and the Deco password. They never go to the public repo.

### Project layout

```
run.py / run.bat / run.sh   entry points, direct launch (no updater)
launcher.py                 auto-updating launcher (supervises run.py)
run_launcher.bat / .sh      entry points for the launcher (Windows / Linux)
set_password.py             set the dashboard password (writes the hash to config.local.yaml)
config.yaml                 all settings + known devices + watched services (tracked — don't edit on a deployed machine)
config.local.yaml.example   template for per-machine overrides (copy to config.local.yaml, untracked)
requirements.txt
app/
  main.py       FastAPI app + API endpoints + page routes + login middleware
  auth.py       dashboard auth: PBKDF2 hashing, sessions, brute-force guard (stdlib only)
  collectors.py background jobs (scan / latency / speedtest / services / outages)
  services.py   down detector: HTTP health checks, official status feeds, service status logic
  deco.py       Deco restart: MAC-list reboot via the main unit's local web interface
  notifiers.py  pluggable alert notifiers (dashboard-only log notifier for now)
  identify.py   auto device identification (mDNS / NetBIOS / rDNS / OUI)
  oui.json      offline MAC-prefix -> vendor table (built from the IEEE list)
  network.py    cross-platform ping sweep + ARP parsing + MAC utils
  db.py         SQLite schema + queries
  wol.py        Wake-on-LAN magic packet
  updates.py    update-check trigger + status file (dashboard <-> launcher bridge)
  config.py     config loading (defaults + config.yaml + config.local.yaml merge)
static/
  index.html    Home page
  devices.html  Devices page
  speed-tests.html  Speed Tests page
  statistics.html   Statistics page
  services.html Services page
  system.html   System page (network controls — Deco restart)
  style.css     shared stylesheet for all dashboard pages
  app.js        shared JS: nav, charts, header/footer status, update check, Deco restart
  login.html    the login page (no external assets — works with zero internet)
tests/
  smoke_test.py boots the server, hits every page and API endpoint, asserts 200s
  test_deco.py  Deco restart unit tests (mocked Deco client)
data/
  monitor.db    created on first run (gitignored)
```

## Requirements

- A **Windows 10/11 PC that's always on** (the monitor runs there; a
  Linux/macOS machine also works via `run_launcher.sh` / `run.sh`, but
  the reference deployment is Windows).
- **Python 3.12+** — [python.org](https://www.python.org/downloads/).
  On the first installer screen, tick **"Add python.exe to PATH"**.
- **Git** — the launcher's auto-updater needs it
  ([git-scm.com](https://git-scm.com/)). Without git, the launcher still
  runs and supervises the monitor, it just can't check for updates.
- A **TP-Link Deco mesh is needed only for the restart feature**.
  Everything else — device tracking, latency, speed tests, outages, the
  down detector, Wake-on-LAN — works on any LAN.

## Quick start

1. **Clone the repo** (or download it) somewhere permanent, e.g.
   `C:\Tools\North-Star`:
   ```bat
   git clone https://github.com/t1-research/North-Star.git
   cd North-Star\network-monitor
   ```
2. **Start the launcher** — double-click `run_launcher.bat` (or
   `run_launcher.sh` on Linux/macOS). It creates the venv, installs the
   dependencies from `requirements.txt`, and starts the monitor. Leave
   the launcher window running; it supervises the monitor from here on.
   (`run.bat` / `python run.py` starts the monitor directly, without the
   updater.)
3. **Open the dashboard:** http://localhost:8080 on the PC itself, or
   `http://<pc-lan-ip>:8080` from any device on your LAN (see
   [Field deployment reference](#field-deployment-reference) for the
   Windows Firewall rule other devices need).
4. **Set a dashboard password** (recommended — the dashboard is open to
   anyone on your LAN until you do):
   ```bat
   python set_password.py
   ```
   It prompts for a password (twice, hidden, at least 8 characters) and
   writes only the hash to `config.local.yaml`. **Restart the monitor**
   afterwards — the running monitor only reads config at startup (stop
   the launcher with Ctrl+C and start it again).
5. **Make it yours:** copy `config.local.yaml.example` to
   `config.local.yaml` and set your subnet, services, and intervals
   (see [Configuration](#configuration)). The shipped `config.yaml`
   describes the reference deployment below; change the subnet and
   latency targets to match your network.
6. **Start on boot** (optional): point a Windows Task Scheduler task at
   `run_launcher.bat` — Create Task, trigger **At startup**, action
   "Start a program" with `run_launcher.bat` and "Start in" set to the
   `network-monitor` folder, untick "Stop the task if it runs longer
   than…". Tick "Run whether user is logged on or not"; highest
   privileges are NOT needed.

From here on the launcher keeps the monitor up to date on its own: every
5 minutes it checks GitHub, and when a push lands it pulls and restarts
the monitor. The dashboard footer shows the running version (commit) and
when the next check is due; the Check for updates button runs a check
immediately.

## Configuration

Settings come from three layers, merged in order (later wins):
`app/config.py` defaults → `config.yaml` (tracked) →
`config.local.yaml` (untracked, per-machine).

### config.yaml — the shipped settings

| Setting | Default (shipped) | What it does |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8080` | Dashboard bind address and port |
| `subnet` | `192.168.68.0/24` | Subnet the device scan sweeps |
| `scan_interval_seconds` | `120` | Device scan cadence |
| `latency_interval_seconds` | `60` | Latency probe cadence |
| `speedtest_interval_seconds` | `900` | Automatic speed test cadence (every 15 min) |
| `service_check_interval_seconds` | `120` | Down detector cadence (per-service `interval_seconds` overrides it) |
| `latency_targets` | `1.1.1.1`, `8.8.8.8`, `192.168.68.1`, `192.168.1.1` | Ping targets; the public ones drive outage detection |
| `services` | 8 services | Sites for the down detector (see below) |
| `devices` | 1 nameless WoL entry | Known devices: manual names (always win over auto-discovery) and WoL buttons |
| `deco` | 3 units, main at `192.168.68.1` | Deco mesh for the System-page restart (see [Restart Deco](#restart-deco-system-page)) |
| `auto_update` / `update_check_interval_seconds` | `true` / `300` | Launcher auto-update behavior |

The `services` list names the sites the household actually uses — the
shipped list is two Vercel sites (checked once a day, to keep Vercel
usage down) plus Google, GitHub, Steam, Discord, YouTube, and Cloudflare
(checked every 2 minutes). Each service may set its own
`interval_seconds`, a `status_api_url` (the vendor's official Statuspage
feed — GitHub, Discord, and Cloudflare publish one), and a
`downdetector_url` (display-only link on the card).

The `devices` list is for overrides only: most devices name themselves
via auto-identification, so only add an entry to rename something or to
enable Wake-on-LAN. Entries match by MAC first (survives DHCP changes),
then by IP. A nameless entry is still probed — it exists only to enable
WoL, and the dashboard shows its discovered name.

### config.local.yaml — your machine's settings

Copy `config.local.yaml.example` to `config.local.yaml` and put your
overrides there: your subnet, your services, your intervals, device
names, the dashboard password hash (written by `set_password.py`), and
the Deco password. It deep-merges over `config.yaml`: nested settings
merge key-by-key; lists and single values replace — except `devices`,
which merges entry by entry, so you only list the devices you want to
add or change.

**⚠️ The one rule: edit `config.local.yaml`, never `config.yaml`, on a
deployed machine.** `config.yaml` is tracked by git, and the launcher
pulls with `git pull --ff-only` — any edits to tracked files make every
pull fail, and updates just stop. `config.local.yaml` is untracked and
ignored by git, so pulls never conflict with it.

## Restart Deco (System page)

The System page mirrors the Deco app's own reboot screen: a checkbox
list of the mesh units (name, Main badge, MAC), a Select all / Deselect
all toggle, and a **Restart selected (N)** button with a live count.
Only the checked units restart — pick both satellites and the main
never reboots.

How it works: the monitor logs into the MAIN unit's local web interface
(via the `tplinkrouterc6u` package) and sends one reboot call naming
only the selected units' MACs. That is the same mechanism the Deco app's
own "Select Deco" screen uses — the Deco API reboots by MAC list, so a
subset restarts just those units. Units are addressed by MAC, so
satellite IP addresses are not needed (the per-unit `host` fields in
`config.yaml` are reference only). The main unit's row warns that it
restarts the whole network — restarting it drops everything for ~1–2
minutes (it's the router), and the monitor logs that as an outage, which
is expected. Restarting satellites only drops the clients attached to
them; the rest of the network stays up.

Setup (one time):

1. Add the Deco password to `config.local.yaml` — the password you use
   to sign into the Deco app:
   ```yaml
   deco:
     password: "your Deco app password"
   ```
   Until this is set, the System page shows the units but the restart
   button stays off. Never put the password in `config.yaml` (tracked
   file, public repo).
2. Check the `deco.units` list in `config.yaml` matches your mesh: one
   entry per unit with its name, MAC, and `main: true` on the main unit
   only. The MACs are in the Deco app (unit details) and on the
   monitor's Devices page. A unit with no MAC is skipped (not shown as
   restartable).
3. **Restart the monitor** (the running monitor only reads config at
   startup). If you run the launcher, stop it (Ctrl+C) and start it
   again — it installs `tplinkrouterc6u` on the next child start.

Notes:

- **First-run caution:** the WB10800 is not on `tplinkrouterc6u`'s
  tested list, so the first restart is a live test. If the firmware
  answers differently, the dashboard says so (login failed vs
  unreachable vs unexpected response) instead of failing silently.
  Test it on one satellite first.
- **Tested hardware:** TP-Link Deco WB10800 (BE3600, Wi-Fi 7), 3-node
  mesh — Living Room (Main), Bedroom, Main Bedroom. All three units run
  firmware 1.0.13 Build 20241213 Rel. 38543 (the latest TP-Link offers for
  V1 hardware as of Nov 2026). Fielded here; the
  first live reboot test is still pending. Other Deco models using the
  same local web API may work (the underlying `tplinkrouterc6u`
  library lists its own tested models), but they are untested with
  this monitor.
- The Deco web UI allows only one logged-in session at a time; the
  monitor logs in, reboots, and logs out in one short sequence.
- Restarting the main unit also restarts the monitor's own connection
  to the internet — the dashboard will show the outage and recover on
  its own; the System page polls until the Deco is back online.

## Field deployment reference

This is the exact network the monitor is fielded on, top-down — the
reference deployment the shipped `config.yaml` describes. Replicating
on your own network means adapting the addresses below (subnet,
gateway, latency targets, Deco units) to yours.

### Internet + modem/router: Netgear CAX30

- **ISP:** Xfinity, 2 Gig plan (~2,000 Mbps down / ~300 Mbps up).
- **Modem/router:** Netgear CAX30 (DOCSIS 3.1 + Wi-Fi 6 combo, AX2700;
  1× 2.5G Ethernet port + 4× 1G LAN ports), firmware V2.2.4.2.
- **Mode:** router mode — **not** bridge mode. The CAX30 stays the
  edge router; the Deco runs in router mode behind it (double NAT,
  made workable by the DMZ below).
- **Admin:** 192.168.1.1, HTTPS-only, admin password changed from the
  default, settings backup saved.
- **DNS:** 1.1.1.1 / 8.8.8.8 / 9.9.9.9. IPv6 DNS:
  2606:4700:4700::1111 / 2001:4860:4860::8888 / 2620:fe::fe. IPv6 via
  DHCPv6, IPv6 firewall on.
- **Wi-Fi radios: OFF.** The CAX30 does no wireless at all — the Deco
  handles all Wi-Fi. (Hiding the SSID alone does not turn the radios
  off; the radios themselves are disabled.)
- **DMZ → 192.168.1.2**, the Deco main unit's WAN address (DHCP
  reservation on the CAX30). This is what makes inbound connections
  and the Deco's own port handling work through the double NAT.
- **Firewall/hardening:** IPv4 firewall on, port scan / DoS protection
  on, respond-to-ping off, MTU 1500, SIP ALG on, Access Control off,
  port forwarding table empty, UPnP off, DDNS off, VPN server off,
  Traffic Meter off, Ethernet port aggregation disabled.

### Mesh: 3× TP-Link Deco WB10800

- **Hardware:** Deco WB10800 (BE3600, Wi-Fi 7 dual-band: 5 GHz + 2.4
  GHz, MLO, 160 MHz channels), 2× 1 Gbps Ethernet ports per unit.
- **Mode:** router mode behind the CAX30, wireless backhaul.
- **Units:** Living Room (Main, gateway 192.168.68.1) downstairs;
  Bedroom and Main Bedroom satellites upstairs.
- **LAN:** 192.168.68.0/24 — this is the subnet the monitor scans.
- **SSIDs:** "North Star" (main), "South Star" (IoT), plus a guest
  network with a captive portal (1-hour sessions, 10/10 Mbps
  bandwidth limit).
- **Known bottleneck:** every Deco port is 1 Gbps, so the mesh caps
  internet at ~940 Mbps despite the 2 Gig plan. Accepted for now — the
  fix is a main node with a 2.5G port (or a UniFi setup), not a
  settings change.

### Monitor host

- **Host:** Windows 11 gaming PC, wired via Ethernet at
  192.168.68.68, always on. The monitor binds `0.0.0.0:8080`.
- **LAN-only — never port-forward 8080.** Other devices on the LAN
  reach the dashboard at `http://192.168.68.68:8080`.
- **Windows Firewall:** the PC's firewall blocks other LAN devices
  from reaching port 8080 until you allow it. Run this once in
  PowerShell **as Administrator**:
  ```powershell
  New-NetFirewallRule -DisplayName "North Star Monitor" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
  ```
  (`127.0.0.1:8080` only ever works on the PC itself — from another
  device, use the PC's LAN address.)
- **Latency targets** (what the monitor pings every minute):
  `1.1.1.1` and `8.8.8.8` (external — these drive outage detection),
  `192.168.68.1` (Deco gateway), `192.168.1.1` (CAX30).

## How it works

### Automatic device identification

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

### Down detector

Every check is a HEAD request (no body downloaded — keeps usage down on
metered hosts) with a 10-second timeout (stdlib urllib only — no extra
dependency). When HEAD doesn't give a clean up answer, the check falls
back to a GET capped at the first 1 KiB, so the down decision always
rests on a real fetch. A 2xx/3xx final status is up; 4xx/5xx, a timeout,
or a DNS/connection failure is down.

- **Per-service intervals:** each service may set its own
  `interval_seconds`; otherwise it uses the global
  `service_check_interval_seconds` (120s). The Vercel sites are
  checked once a day on purpose — every 2 minutes would run up
  Vercel usage for no benefit.
- **Official status feeds:** GitHub, Discord, and Cloudflare publish
  official status feeds (Atlassian Statuspage JSON), and the monitor
  fetches them on the same cadence as the health check — set via a
  service's `status_api_url` in config.yaml. The card then shows a second
  line, e.g. "GitHub reports: All Systems Operational", plus the first
  open incident name when there is one. **The feed is display only: it
  never drives up/down, down events, or uptime %** — our own health check
  is the source of truth for that. The feed answers the other question:
  "does the vendor itself report a problem?" A failed feed fetch keeps
  the last known state; a service that never fetched successfully simply
  shows no feed line. Each card also carries a small "Downdetector ↗"
  link to the service's Downdetector page (display only — the monitor
  never fetches Downdetector, which has no free API).
- **2-failure rule:** a service is only declared down after 2 consecutive
  failed checks, so a single blip never raises an alarm. It recovers on
  the first success after that. Down windows appear in the event log with
  their duration, and a service that stays down gets exactly one open
  event.
- **Internet vs service:** if the internet itself is down (all external
  latency targets failing), service checks are skipped and every service
  shows as "unknown" — you get one internet outage, not eight false
  service outages. Checks skipped this way don't count against a
  service's 24h uptime %.
- **Dashboard:** the Services page shows a card per service — status dot
  (green up / red down / grey unknown), last response time, and 24h
  uptime. The header pill turns red while any service is down. (Outage
  detection still runs in the background — it powers the Internet status
  card and the unknown-during-outage behavior — but the event log is no
  longer displayed anywhere.)
- **API:** `GET /api/services` returns the current status per service;
  `GET /api/services/history?hours=24` returns the raw check rows.

### Launcher & auto-updates

The launcher works like the Minecraft launcher: it checks for updates,
runs the application, restarts it when a new version lands, and reports
the current version. Start it once and you never manually stop or restart
the monitor again.

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
  The footer also has a Check for updates button (launcher only) that
  drops a trigger file (`data/update_check_requested`) to run a check
  immediately instead of waiting for the next cycle.
- **Dependencies:** before every child start, the launcher checks that
  all required modules import, and runs `pip install -r
  requirements.txt` when one is missing — so a new release that adds a
  dependency installs it on the next restart. A failed install is logged
  as a warning, not fatal.
- **Crashes:** if the monitor exits on its own, the launcher restarts it
  with backoff (5s, 15s, 30s cap). If it fails to stay up 3 times in a row
  after an update, the launcher logs a clear warning that the new version
  may be broken and keeps retrying (no auto-rollback in v1).
- **Disable auto-updates:** set `auto_update: false` in
  `config.local.yaml`. The launcher still supervises the monitor, it just
  stops checking for new commits.
- **No git, no problem:** if git isn't installed or the folder isn't a git
  checkout, the launcher logs a warning once and just runs the monitor
  without update checks. The dashboard footer shows "dev build" instead
  of a commit.

### Login

The dashboard is open to anyone on your LAN until you set a password.
To protect it:

1. From the `network-monitor` folder, run:
   ```bat
   python set_password.py
   ```
   It prompts for a password (twice, hidden, at least 8 characters),
   hashes it with PBKDF2-HMAC-SHA256 (600,000 iterations), and writes
   the hash to `config.local.yaml` — and nothing else in that file is
   touched.
2. **Restart the monitor.** The running monitor only reads config at
   startup, so the password takes effect on the next start. If you run
   the launcher, stop it (Ctrl+C) and start it again.

Notes:

- The hash lives in `config.local.yaml`, which is NOT tracked by git —
  it never goes to the public repo. Never put a hash (or a password) in
  `config.yaml`.
- A signed-in session lasts **7 days**; the Log out button in the nav bar
  signs out immediately. Restarting the monitor signs everyone out
  (sessions live in memory only).
- **Lockout:** 5 failed logins from one IP within 10 minutes locks that
  IP out for 5 minutes (further attempts get a 429). A successful login
  clears the count.
- The dashboard is still plain HTTP, so the password only protects it
  on networks you trust. Remote access should go over Tailscale
  (encrypted) — never a raw port forward, password or not.

### API

| Endpoint | What it does |
|---|---|
| `GET /` | Home dashboard |
| `GET /devices` | Devices page |
| `GET /speed-tests` | Speed Tests page |
| `GET /statistics` | Statistics page |
| `GET /services` | Services page |
| `GET /system` | System page (network controls — Deco restart) |
| `GET /login` | Login page (redirects to `/` when no password is set or already signed in) |
| `POST /api/login` | Sign in with JSON `{"password": "..."}` — sets the 7-day session cookie |
| `POST /api/logout` | Sign out — revokes the session and clears the cookie |
| `GET /api/status` | Snapshot: internet up/down, latest latency, last speed test, device counts, running version (commit SHA) + started_at, Deco restart data |
| `GET /api/devices` | All known devices with online/last-seen, manual name, and discovered name + source |
| `POST /api/devices/sweep-offline` | Delete offline device rows (never config-defined devices) |
| `GET /api/latency?hours=24` | Latency history |
| `GET /api/speedtests?days=7` | Speed test history |
| `GET /api/events` | Outage/event log, newest first |
| `GET /api/services` | Current status per service (up/down/unknown, last response, 24h uptime %) |
| `GET /api/services/history?hours=24` | Per-service check history |
| `POST /api/check-updates` | Ask the launcher to check GitHub for updates now (launcher only; 409 when standalone) |
| `POST /api/speedtest/run` | Start a speed test in the background (202, or 409 if one is running) |
| `POST /api/wol/{mac}` | Send a WoL magic packet |
| `POST /api/deco/reboot` | Restart Deco units: JSON `{"units": ["<name>", ...]}` — only the named units reboot; per-unit results in the response (503 when no Deco password is set, 502 when the Deco refuses or can't be reached) |

## Notes & limits

- **Devices that block ping won't appear.** Some phones/IoT gear ignore ICMP.
  They may still show up if they have an ARP entry, but ping is the main
  discovery method.
- **Per-device bandwidth is not available.** The Deco doesn't expose it
  outside its app. The CAX30 traffic meter and Deco local API are
  experiments for later.
- **The CAX30 (192.168.1.1) isn't in the device table.** It sits on
  another subnet, so it can't be discovered — but it's still pinged every
  minute and charted as a latency target.
- **WoL needs the target's MAC.** For devices listed by IP only, the MAC is
  learned from the first ARP scan — the Wake button appears after that.
  WoL also needs to be enabled in the target's BIOS and network adapter.
- Speed tests saturate the connection for ~20 seconds each (~10s down,
  ~10s up) and move roughly line-speed × 20s of traffic — about 1.5 GB
  per test on a gigabit line. The shipped default is every 15 minutes;
  raise `speedtest_interval_seconds` in config.local.yaml if that bothers
  you, and keep your ISP data cap in mind.
- The Python `speedtest-cli` package underreports on gigabit lines
  (single-connection test + Python overhead — it commonly tops out around
  500–800 Mbps even on a faster line). The numbers are consistent enough
  for trend tracking, but they are not a line-speed benchmark.

## Development

### Running the tests

From the `network-monitor` folder, with the dependencies installed:

```bat
python tests/smoke_test.py        — boots the server, hits every page and API endpoint
python -m pytest tests/test_deco.py   — Deco restart unit tests (mocked Deco client; pip install pytest)
python -m compileall app run.py tests  — byte-compile check, same as CI
```

### CI

GitHub Actions (`.github/workflows/ci-network-monitor.yml`) runs on
every push and pull request touching `network-monitor/**`: it installs
the dependencies and runs the byte-compile check plus the smoke test on
both Ubuntu and Windows (Python 3.12).

### Contributing a change

1. Edit the code, run the smoke test (and `test_deco.py` if you touched
   the Deco code), and check the dashboard in a browser.
2. Push to `main`. The launcher on every deployed install picks the
   change up within 5 minutes and restarts the monitor with it — there
   is no separate release step.
3. If a change adds a dependency, add it to `requirements.txt` — the
   launcher installs missing modules before the next child start, so
   deployed installs get it automatically.

### Adding alerts

Alerting is dashboard-only for now, but the hook is already in place.
`app/notifiers.py` has a tiny registry: `notify(event)` fans an event
dict out to every registered notifier, and the collectors call it on
service down/recovery and outage open/close. An event dict looks like:

```python
{"kind": "service_down",  # or service_recovered / outage_open / outage_closed
 "service": "GitHub",     # None for internet outages
 "url": "https://github.com",
 "ts": 1793000000,
 "note": "human-readable detail"}
```

To add a Discord webhook notifier:

1. In Discord: Server Settings → Integrations → Webhooks → New Webhook,
   copy the URL.
2. Add a `discord_webhook_url` default in `app/config.py` and set the
   URL in `config.local.yaml` (never in `config.yaml` — the webhook URL
   is a secret).
3. In `app/notifiers.py`, write a function that POSTs a JSON
   `{"content": ...}` message to that URL with stdlib urllib, and
   `register()` it. `notify()` already guards each notifier with
   try/except, but a notifier should still never raise on a bad response.

An email notifier works the same way: a function taking the event dict,
sending via `smtplib` with credentials from config (a Gmail app password,
not your real password).

## Status & roadmap

- **Current:** v0.7.0 — six dashboard pages, device tracking with
  auto-identification, latency/speed/outage history, the down detector
  with vendor status feeds, Wake-on-LAN, login, the auto-updating
  launcher, and per-unit Deco restarts from the System page.
- **Alerting is not built yet.** `app/notifiers.py` has a small registry
  — `notify(event)` fans an event dict out to every registered notifier,
  and the collectors already call it on service down/recovery and outage
  open/close. The next step is a Discord webhook and/or email notifier
  implementation on top of that hook.
- **Ideas, not built yet:** Deco local API experiments (block/unblock
  devices, per-device bandwidth — reverse-engineered, may break on
  firmware updates); DNS server mode (Pi-hole-style ad blocking +
  per-device DNS logs).
