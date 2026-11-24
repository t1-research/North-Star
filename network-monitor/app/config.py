"""Configuration loading for North Star Monitor.

Reads config.yaml next to the project root and fills in sane defaults for any
missing keys, so the app still starts on a partial config file.

Per-machine settings belong in config.local.yaml (same folder, NOT tracked
by git — copy config.local.yaml.example to get started). It is deep-merged
over config.yaml, so your edits never touch a tracked file and the
launcher's `git pull` never conflicts with them. Rule: edit
config.local.yaml on your PC, never config.yaml.
"""
from __future__ import annotations

import copy
import os
import subprocess

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
LOCAL_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.local.yaml")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "monitor.db")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "subnet": "192.168.68.0/24",
    "scan_interval_seconds": 120,
    "latency_interval_seconds": 60,
    "speedtest_interval_seconds": 3600,
    "service_check_interval_seconds": 120,
    "latency_targets": ["1.1.1.1", "8.8.8.8", "192.168.68.1", "192.168.1.1"],
    "devices": [],
    "services": [],
    # Launcher / auto-updates (launcher.py only; ignored by run.py).
    "auto_update": True,
    "update_check_interval_seconds": 300,
    "update_branch": "main",
    "update_remote": "origin",
}


def _device_key(entry: dict):
    """Identity of a device entry: MAC first, then IP, then name."""
    if entry.get("mac"):
        return ("mac", str(entry["mac"]).upper())
    if entry.get("ip"):
        return ("ip", str(entry["ip"]))
    return ("name", str(entry.get("name", "")))


def _merge_devices(base_list: list, override_list: list) -> list:
    """Merge two device lists entry by entry (matched by MAC, then IP, then
    name): a local entry with the same identity replaces that one device,
    new entries are appended. So config.local.yaml only needs to list the
    devices you want to add or change — the rest of the tracked list stays."""
    merged = list(base_list)
    index = {_device_key(e): i for i, e in enumerate(merged)}
    for entry in override_list:
        key = _device_key(entry)
        if key in index:
            merged[index[key]] = entry
        else:
            index[key] = len(merged)
            merged.append(entry)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into `base` in place and return it. Nested dicts
    merge key-by-key; lists and scalars replace — except `devices`, which
    merges entry by entry (see _merge_devices)."""
    for key, value in override.items():
        if (key == "devices" and isinstance(value, list)
                and isinstance(base.get(key), list)):
            base[key] = _merge_devices(base[key], value)
        elif (key in base and isinstance(base[key], dict)
                and isinstance(value, dict)):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load config.yaml merged over DEFAULTS, then config.local.yaml merged
    over that (local overrides win)."""
    cfg = copy.deepcopy(DEFAULTS)
    _deep_merge(cfg, _load_yaml_file(path))
    _deep_merge(cfg, _load_yaml_file(LOCAL_CONFIG_PATH))
    return cfg


# ------------------------------------------------------------- git helpers

def find_repo_root(start: str):
    """Walk up from `start` until a directory containing `.git` is found
    (a directory, or a file for worktrees/submodules). Returns None outside
    a git checkout."""
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def git_short_sha() -> str:
    """Short commit SHA of the checkout containing this app, or "dev" when
    git is unavailable or this isn't a git checkout."""
    root = find_repo_root(PROJECT_ROOT)
    if root is None:
        return "dev"
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "dev"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "dev"


def build_device_lookups(cfg: dict):
    """Build MAC -> entry and IP -> entry lookup dicts from config devices.

    MACs are normalized to uppercase colon form (AA:BB:CC:DD:EE:FF).
    """
    from .network import normalize_mac

    by_mac: dict[str, dict] = {}
    by_ip: dict[str, dict] = {}
    for entry in cfg.get("devices", []):
        if entry.get("mac"):
            by_mac[normalize_mac(entry["mac"])] = entry
        if entry.get("ip"):
            by_ip[entry["ip"]] = entry
    return by_mac, by_ip
