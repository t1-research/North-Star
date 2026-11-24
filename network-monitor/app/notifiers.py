"""Pluggable alert notifiers for North Star Monitor.

Dashboard-only for now: the only registered notifier logs events to stdout,
so down/recovery moments are visible in the server log and on the dashboard.
When Discord/email alerting is wanted later, write a function that takes one
event dict and register it here — see the README's "Adding alerts" section.

An event dict looks like:
    {"kind": "service_down" | "service_recovered" | "outage_open" | "outage_closed",
     "service": "GitHub" | None, "url": "https://github.com" | None,
     "ts": 1793000000, "note": "human-readable detail"}
"""
from __future__ import annotations

_notifiers: list = []


def register(fn) -> None:
    """Register a notifier callable. It receives one event dict."""
    _notifiers.append(fn)


def notify(event: dict) -> None:
    """Fan an event out to every registered notifier.

    One broken notifier must never kill the collector loop, so each call
    is guarded.
    """
    for fn in _notifiers:
        try:
            fn(event)
        except Exception as exc:  # noqa: BLE001 — log and continue
            print(f"[notify] {getattr(fn, '__name__', fn)} failed: {exc}", flush=True)


def _log_notifier(event: dict) -> None:
    label = event.get("service") or "internet"
    print(f"[notify] {event['kind']}: {label} — {event.get('note', '')}", flush=True)


register(_log_notifier)
