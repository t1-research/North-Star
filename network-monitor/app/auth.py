"""Dashboard authentication for North Star Monitor.

Stdlib only: PBKDF2 password hashing, in-memory sessions, and a small
brute-force guard. The password hash lives in config.local.yaml
(`dashboard_password_hash`) — never in the tracked config.yaml, which is
in a public repo.

Sessions live in memory only, so restarting the monitor logs everyone
out. That is fine for a single-user home dashboard.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# Password hashing: PBKDF2-HMAC-SHA256, ~600k iterations, 16-byte random
# salt per password. Stored as: pbkdf2_sha256$iterations$salt_hex$hash_hex
ITERATIONS = 600_000
SALT_BYTES = 16

# Sessions last 7 days; the cookie's Max-Age matches.
SESSION_TTL_SECONDS = 7 * 24 * 3600
COOKIE_NAME = "northstar_session"

# Brute-force guard: 5 failed logins from one IP within 10 minutes locks
# that IP out for 5 minutes (attempts during lockout get 429).
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 10 * 60
LOCKOUT_SECONDS = 5 * 60

_sessions: dict[str, float] = {}       # token -> expiry (epoch seconds)
_attempts: dict[str, list[float]] = {}  # ip -> failed attempt timestamps
_lockouts: dict[str, float] = {}        # ip -> lockout expiry (epoch seconds)


def hash_password(password: str) -> str:
    """Hash a password for storage. Returns the stored-format string."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. Any malformed stored value
    simply fails — it never raises."""
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- sessions

def create_session() -> str:
    """Create a session and return its token."""
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def validate_session(token: str | None) -> bool:
    """True if the token names a live session. Expired sessions are
    dropped the first time they are seen."""
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry < time.time():
        del _sessions[token]
        return False
    return True


def revoke_session(token: str | None) -> None:
    """Log a session out. Unknown tokens are a silent no-op."""
    if token:
        _sessions.pop(token, None)


# ------------------------------------------------------- brute-force guard

def is_locked_out(ip: str) -> bool:
    """True if this IP is inside a lockout window."""
    until = _lockouts.get(ip)
    if until is None:
        return False
    if until < time.time():
        del _lockouts[ip]
        return False
    return True


def record_failed_login(ip: str) -> None:
    """Note a failed login from `ip`; trip the lockout once the count in
    the window hits the limit."""
    now = time.time()
    recent = [t for t in _attempts.get(ip, [])
              if now - t < ATTEMPT_WINDOW_SECONDS]
    recent.append(now)
    _attempts[ip] = recent
    if len(recent) >= MAX_ATTEMPTS:
        _lockouts[ip] = now + LOCKOUT_SECONDS
        _attempts[ip] = []


def record_successful_login(ip: str) -> None:
    """A good login clears the failed-attempt history for this IP."""
    _attempts.pop(ip, None)
    _lockouts.pop(ip, None)
