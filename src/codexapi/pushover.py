"""Pushover notification helper."""

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from .rate_limits import quota_line

_PUSHOVER_PATH = "~/.pushover"
_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_MAX_MESSAGE = 1024

_STARTUP_MESSAGE = (
    "Pushover user and app keys read, notifications for task and science enabled."
)


class Pushover:
    """Send Pushover notifications when configured."""

    _lock = threading.Lock()
    _state = {}

    def __init__(self, path=_PUSHOVER_PATH):
        self.path = os.path.expanduser(path)
        self._state_ref = self._state_for_path(self.path)

    def ensure_ready(self, announce=True):
        state = self._state_ref
        with self._lock:
            if not state["checked"]:
                state["checked"] = True
                if not os.path.exists(self.path):
                    state["enabled"] = False
                    return False
                try:
                    tokens = _load_pushover_tokens(self.path)
                except ValueError as exc:
                    state["error"] = f"Pushover config error: {exc}"
                    raise SystemExit(state["error"]) from None
                state["tokens"] = tokens
                state["enabled"] = True
            if state["error"]:
                raise SystemExit(state["error"]) from None
            if announce and state["enabled"] and not state["announced"]:
                print(_STARTUP_MESSAGE)
                state["announced"] = True
            return state["enabled"]

    def send(self, title, message):
        tokens = self._get_tokens()
        if not tokens:
            return False
        user_key, app_token = tokens
        title_text = _single_line(title).strip() or "Codex update"
        message_text = (message or "").strip()
        if not message_text:
            return False
        message_text = _append_quota_line(message_text)
        message_text = _truncate(message_text, _MAX_MESSAGE)
        payload = urllib.parse.urlencode(
            {
                "token": app_token,
                "user": user_key,
                "title": title_text,
                "message": message_text,
            }
        ).encode("utf-8")
        request = urllib.request.Request(_PUSHOVER_URL, data=payload)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            _report_pushover_error(body, exc.code)
            return False
        except Exception as exc:
            _warn(f"Pushover notification failed: {exc}")
            return False
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            _warn("Pushover returned invalid JSON.")
            return False
        if data.get("status") != 1:
            _report_pushover_error(body, None)
            return False
        return True

    def _get_tokens(self):
        if not self.ensure_ready(announce=False):
            return None
        return self._state_ref["tokens"]

    @classmethod
    def _state_for_path(cls, path):
        state = cls._state.get(path)
        if state is None:
            state = {
                "checked": False,
                "enabled": False,
                "tokens": None,
                "error": None,
                "announced": False,
            }
            cls._state[path] = state
        return state


def _load_pushover_tokens(path):
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    if len(lines) != 2:
        raise ValueError(
            f"{path} must contain two non-empty lines: user key then app token"
        )
    return lines[0], lines[1]


def _report_pushover_error(body, status_code):
    errors = None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            errors = data.get("errors")
    except json.JSONDecodeError:
        errors = None
    message = "Pushover notification failed."
    if status_code:
        message = f"{message} HTTP {status_code}."
    detail = _format_pushover_errors(errors)
    if detail:
        message = f"{message} {detail}"
    _warn(message)


def _format_pushover_errors(errors):
    if not errors:
        return ""
    if isinstance(errors, str):
        errors = [errors]
    if not isinstance(errors, list):
        return ""
    cleaned = [str(error).strip() for error in errors if str(error).strip()]
    if not cleaned:
        return ""
    hint = []
    lower = " ".join(cleaned).lower()
    if "user" in lower:
        hint.append("Check the user key on line 1 of ~/.pushover.")
    if "token" in lower or "application" in lower:
        hint.append("Check the app token on line 2 of ~/.pushover.")
    if "message" in lower:
        hint.append("Check that the message is not empty or too long.")
    suffix = " ".join(hint)
    if suffix:
        return f"{'; '.join(cleaned)} {suffix}"
    return "; ".join(cleaned)


def _single_line(text):
    if not text:
        return ""
    return " ".join(str(text).replace("\r", " ").split())


def _truncate(text, limit):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _warn(message):
    print(message, file=sys.stderr)


def _append_quota_line(message):
    line = quota_line()
    if not line:
        return message
    return f"{message}\n{line}"
