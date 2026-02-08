"""Periodic watch loop for long-running Codex work.

watch keeps a single Codex thread alive and periodically "ticks" it with the
current time and a reminder of the original instructions. Each tick expects a
small JSON status payload so the loop can decide whether to continue.
"""

import json
import time
from datetime import datetime

from .agent import Agent

_JSON_INSTRUCTIONS = (
    "Respond with JSON only (no markdown/backticks/extra text).\n"
    "Return a single JSON object with keys:\n"
    "  status: string (one line)\n"
    "  continue: boolean\n"
    "  comments: string\n"
    "To stop this watch loop, set continue to false."
)


def watch(minutes, prompt, cwd=None, yolo=True, flags=None):
    """Run a periodic watch loop.

    Args:
        minutes: Tick interval in whole minutes (>= 1).
        prompt: The original instruction prompt.
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        flags: Additional raw CLI flags to pass to Codex.

    Returns:
        The last parsed JSON status object.
    """
    if not isinstance(minutes, int):
        raise TypeError("minutes must be an integer")
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    interval = minutes * 60
    session = Agent(cwd, yolo, None, flags)

    last_sent = None
    last_result = None
    tick = 0

    while True:
        tick += 1
        sent_at = time.monotonic()
        elapsed = None if last_sent is None else sent_at - last_sent
        last_sent = sent_at

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        message = _build_tick_prompt(prompt, now, elapsed, tick)
        output = session(message)
        result = _parse_status(output)
        last_result = result
        _print_status(now, elapsed, tick, result)

        if not result["continue"]:
            return last_result

        next_tick = sent_at + interval
        sleep_seconds = next_tick - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def _build_tick_prompt(prompt, now, elapsed, tick):
    lines = [
        f"Tick {tick}.",
        f"Local time now: {now}",
    ]
    if elapsed is not None:
        lines.append(
            "Time since last tick: "
            f"{_format_minutes_seconds(elapsed)} ({int(round(elapsed))}s)"
        )
    lines.extend(
        [
            "",
            "A reminder: your instructions are:",
            prompt.strip(),
            "",
            _JSON_INSTRUCTIONS,
        ]
    )
    return "\n".join(lines).strip()


def _format_minutes_seconds(seconds):
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    if seconds < 0:
        seconds = 0
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s"


def _parse_status(output):
    text = _maybe_strip_code_fence(str(output or "").strip())
    data = _try_parse_json(text)
    if data is None:
        snippet = text[:200].replace("\n", "\\n")
        raise ValueError(f"Invalid JSON response. Snippet: {snippet}")
    if not isinstance(data, dict):
        raise ValueError("Status JSON must be an object.")

    status = data.get("status")
    cont = data.get("continue")
    comments = data.get("comments")

    if not isinstance(status, str):
        raise ValueError("Status JSON missing string 'status'.")
    if not isinstance(cont, bool):
        raise ValueError("Status JSON missing boolean 'continue'.")
    if comments is None:
        comments = ""
    if not isinstance(comments, str):
        raise ValueError("Status JSON missing string 'comments'.")

    return {
        "status": _single_line(status),
        "continue": cont,
        "comments": comments,
    }


def _maybe_strip_code_fence(text):
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if not lines:
        return text
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _try_parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _single_line(text):
    return " ".join(text.replace("\r", " ").split())


def _print_status(now, elapsed, tick, result):
    delta = ""
    if elapsed is not None:
        delta = f" +{_format_minutes_seconds(elapsed)}"
    status = result.get("status", "")
    cont = result.get("continue")
    line = f"[watch {tick} {now}{delta}] {status} (continue={cont})".rstrip()
    print(line)
    comments = result.get("comments") or ""
    if comments.strip():
        print(comments.rstrip())

