"""Helpers for reading Codex rate limits from session logs."""

import json
import os
import time
from pathlib import Path

_QUOTA_PREFIX = "Limits:"


def rate_limits():
    """Return the latest rate_limits dict from Codex session logs."""
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    sessions = root / "sessions"
    if not sessions.exists():
        return None
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(sessions):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            candidates.append((mtime, path))
    if not candidates:
        return None
    for _mtime, path in sorted(candidates, reverse=True):
        found = _extract_rate_limits(path)
        if found is not None:
            return found
    return None


def _extract_rate_limits(path):
    last = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                rate_data = payload.get("rate_limits")
                if isinstance(rate_data, dict):
                    last = rate_data
    except OSError:
        return None
    return last


def quota_line():
    """Return a human-readable quota line."""
    data = rate_limits()
    if not data:
        return f"{_QUOTA_PREFIX} unavailable"
    primary = data.get("primary") or {}
    secondary = data.get("secondary") or {}
    primary_left = _percent_left(primary.get("used_percent"))
    secondary_left = _percent_left(secondary.get("used_percent"))
    primary_reset = _format_reset(primary.get("resets_at"))
    secondary_reset = _format_reset(secondary.get("resets_at"))
    if primary_left is None or secondary_left is None:
        return f"{_QUOTA_PREFIX} unavailable"
    return (
        f"{_QUOTA_PREFIX} {primary_left}% / {secondary_left}% left "
        f"(reset in {primary_reset} / {secondary_reset})"
    )


def _percent_left(used_percent):
    if not isinstance(used_percent, (int, float)):
        return None
    left = 100.0 - float(used_percent)
    if left < 0:
        left = 0.0
    if left > 100:
        left = 100.0
    return int(round(left))


def _format_reset(resets_at):
    if not isinstance(resets_at, (int, float)):
        return "unknown"
    remaining = float(resets_at) - time.time()
    if remaining < 0:
        remaining = 0
    hours = remaining / 3600.0
    if hours > 24:
        days = hours / 24.0
        return f"{int(round(days))}d"
    return f"{int(round(hours))}h"
