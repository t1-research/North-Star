# North Star

A self-hosted home infrastructure platform — network monitoring, CI/CD, and
dev services, built from scratch in Python.

## Projects

- **network-monitor/** — North Star Monitor: live device tracking, latency
  graphs, speed test history, outage logging, and Wake-on-LAN on one web
  dashboard. Runs on the always-on gaming PC. See
  [network-monitor/README.md](network-monitor/README.md) for setup.

More services coming — this repo is the home for everything running the home
lab.

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
runs a smoke test — starts the server and hits every API endpoint plus the
dashboard — on both Ubuntu and Windows.
