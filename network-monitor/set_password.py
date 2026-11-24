"""Set the dashboard password for North Star Monitor.

    python set_password.py

Prompts for a password (twice, hidden), hashes it, and writes ONLY the
`dashboard_password_hash` key into config.local.yaml — every other key in
that file is preserved. Restart the monitor for the change to take
effect: the running monitor only reads config at startup, so if you run
the launcher, stop it (Ctrl+C) and start it again.

The hash lives in config.local.yaml, which is NOT tracked by git — it
never goes to the public repo. Nothing here prints the password or the
hash.
"""
import getpass
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.auth import hash_password  # noqa: E402
from app.config import LOCAL_CONFIG_PATH  # noqa: E402

MIN_LENGTH = 8


def main() -> int:
    password = getpass.getpass("New dashboard password: ")
    if len(password) < MIN_LENGTH:
        print(f"Password must be at least {MIN_LENGTH} characters.")
        return 1
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1
    cfg: dict = {}
    if os.path.exists(LOCAL_CONFIG_PATH):
        with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    cfg["dashboard_password_hash"] = hash_password(password)
    with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    print("Password set. Restart the monitor for it to take effect — "
          "if you run the launcher, stop it (Ctrl+C) and start it again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
