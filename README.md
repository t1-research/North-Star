# North Star

A self-hosted home infrastructure platform — network monitoring, CI/CD, and
dev services, built from scratch in Python. One repo, one always-on PC,
everything that runs the home lab.

## Projects

- **network-monitor/** — North Star Monitor: a home network monitoring
  dashboard. Live device tracking with automatic identification, latency
  graphs, speed test history, outage logging, a down detector for the
  services the household uses, Wake-on-LAN, and per-unit restarts of the
  Deco mesh — on six dashboard pages (Home, Devices, Speed Tests,
  Statistics, Services, System). It ships with an auto-updating launcher:
  push a change and the running monitor restarts itself with the new
  code. See [network-monitor/README.md](network-monitor/README.md) for
  the full setup guide, including the
  [field deployment reference](network-monitor/README.md#field-deployment-reference)
  — the exact network (CAX30 → Deco mesh → monitor host) it is fielded
  on.

More services coming — this repo is the home for everything running the
home lab.

## Layout

```
network-monitor/   the monitor: FastAPI app, SQLite storage, launcher, tests
.github/workflows/ CI (smoke test on Ubuntu + Windows)
```

## Auto-updates

The monitor ships with a launcher (`network-monitor/launcher.py`, started
with `run_launcher.bat` on Windows) that supervises the app and pulls new
commits from GitHub automatically — push a change and the running monitor
restarts itself with the new code, Minecraft-launcher style. Per-machine
settings live in the untracked `config.local.yaml` so pulls never conflict
with local edits.

## CI

GitHub Actions runs on every push and pull request. The network monitor job
(`.github/workflows/ci-network-monitor.yml`) installs the dependencies and
runs a byte-compile check plus a smoke test — starts the server and hits
every page and API endpoint — on both Ubuntu and Windows (Python 3.12).
