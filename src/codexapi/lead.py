"""Periodic lead loop for long-running agent work.

lead keeps a single agent thread alive and periodically checks in with the
current time and a reminder of the original instructions. Each check-in expects
a small JSON status payload so the loop can decide whether to continue. When a
leadbook is enabled, its contents are injected into each check-in and must be
updated before the agent responds.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

from .agent import Agent
from .pushover import Pushover

_WELCOME_PROMPT = (
    "Welcome. You are the lead. You have authority to take action, allocate resources, and move work forward. "
    "This loop exists to extend your reach, not to restrict you. Your job is to understand the real situation, "
    "interpret the intent behind the goals, and move reality toward them. If the world is not moving, treat that "
    "as evidence and reconsider your frame rather than merely reporting stasis. If progress is possible, take it. "
    "If you are blocked, name the blocker and the next best action to remove it.\n"
    "The instructions below are a map, not a cage. Follow them, but use judgment when they are incomplete or "
    "conflicting. You are responsible for results.\n"
    "Please follow the instructions completely and take all the actions you deem useful at the current time before "
    "responding to the user. Each time you respond to the user, the "
    "system will wait for {minutes} minutes and will then wake you up to check for any changes or progress and continue "
    "your work. Every reply must be JSON in the specific format described at the end of this message."
)
_JSON_INSTRUCTIONS = (
    "Respond with JSON only (no markdown/backticks/extra text).\n"
    "Return a single JSON object with keys:\n"
    "  status: string (one line)\n"
    "  continue: boolean\n"
    "  comments: string (optional)\n"
    "To stop this lead loop, set continue to false."
)
_LEADBOOK_INSTRUCTIONS = (
    "Update the leadbook before responding. Add or revise dated notes when your picture, "
    "assumptions, or decisions changed. This is your working page—where you think, probe, "
    "decide, and reframe the work when needed. Keep it useful; do not pad it with diary "
    "entries just to satisfy the loop."
)
_LEADBOOK_TEMPLATE = """# Leadbook — Studio Notes

This is the working page for the lead loop. Append a new entry every check-in.
Keep it short and concrete.

## 2026-02-17 09:10
Aim:
- <what you are trying to move forward>

What I looked at:
- <files, issues, logs, commands>

Signals:
- <facts that matter>

Threads I pulled:
- <questions you chased>

Turns:
- <what changed your view>

Decision & Next Move:
- <what you will do next>
"""
_DATED_NOTE_RE = re.compile(r"(?m)^#{2,3}\s+\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?:\s*UTC)?)?")
_LEADBOOK_LIMIT = 2000
_LEADBOOK_HEADER_LIMIT = 900
_LEADBOOK_TAIL_LIMIT = 1200


def lead(
    minutes,
    prompt,
    cwd=None,
    yolo=True,
    flags=None,
    leadbook=None,
    backend=None,
    fast=False,
):
    """Run a periodic lead loop.

    Args:
        minutes: Check-in interval in whole minutes (>= 0).
        prompt: The original instruction prompt.
        cwd: Optional working directory for the agent session.
        yolo: Whether to pass --yolo to the agent backend.
        flags: Additional raw CLI flags to pass to the agent backend.
        leadbook: Optional path to the leadbook file. Set to False to disable.
        backend: Agent backend to use ("codex" or "cursor").
        fast: Enable Codex fast mode. Defaults to normal mode.

    Returns:
        The last parsed JSON status object.
    """
    if not isinstance(minutes, int):
        raise TypeError("minutes must be an integer")
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    interval = minutes * 60
    if fast:
        session = Agent(cwd, yolo, None, flags, backend=backend, fast=True)
    else:
        session = Agent(cwd, yolo, None, flags, backend=backend)
    pushover = Pushover()
    pushover.ensure_ready()
    title = _format_title(prompt)
    leadbook_path = _resolve_leadbook_path(leadbook, cwd)
    if leadbook_path:
        _ensure_leadbook(leadbook_path)

    last_sent = None
    last_result = None
    tick = 0

    while True:
        tick += 1
        sent_at = time.monotonic()
        elapsed = None if last_sent is None else sent_at - last_sent
        last_sent = sent_at

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        leadbook_snapshot = _snapshot_leadbook(leadbook_path)
        message = _build_tick_prompt(
            prompt,
            now,
            elapsed,
            tick,
            minutes,
            leadbook_path,
            leadbook_snapshot["text"],
        )
        output = session(message)
        try:
            result = _parse_status(output)
        except ValueError as exc:
            print(
                f"[lead {tick} {now}] Invalid JSON from agent, requesting retry: {exc}",
                file=sys.stderr,
            )
            retry_prompt = _json_retry_prompt(prompt, tick, str(exc), output)
            retry_output = session(retry_prompt)
            try:
                result = _parse_status(retry_output)
            except ValueError as exc2:
                details = _format_json_double_failure(
                    str(exc),
                    output,
                    str(exc2),
                    retry_output,
                )
                pushover.send(title, f"Lead stopped (invalid JSON).\n{details}")
                raise RuntimeError(
                    "Agent was unable to provide valid JSON output after retry.\n"
                    + details
                ) from None
        last_result = result
        _print_status(now, elapsed, tick, result)

        if not result["continue"]:
            pushover.send(title, _format_stop_message(tick, now, result))
            return last_result

        if interval > 0:
            next_tick = sent_at + interval
            sleep_seconds = next_tick - time.monotonic()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def _build_tick_prompt(prompt, now, elapsed, tick, minutes, leadbook_path, leadbook):
    lines = []

    if tick == 1:
        lines.extend(
            [
                _WELCOME_PROMPT.format(minutes=minutes),
                "",
            ]
        )

    lines.extend(
        [
            f"Check-in {tick}.",
            f"Local time now: {now}",
        ]
    )
    if elapsed is not None:
        lines.append(
            "Time since last check-in: "
            f"{_format_minutes_seconds(elapsed)} ({int(round(elapsed))}s)"
        )
    lines.extend(
        [
            "",
            "A reminder: your instructions are:",
            prompt.strip(),
        ]
    )
    leadbook_block = _leadbook_block(leadbook_path, leadbook)
    if leadbook_block:
        lines.extend(["", leadbook_block])
    lines.extend(["", _JSON_INSTRUCTIONS])
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


def _json_retry_prompt(prompt, tick, error, output):
    snippet = _snippet(output, 600)
    lines = [
        f"Your last message (check-in {tick}) was not valid JSON.",
        f"Error: {error}",
        "",
        "Here is your previous output (truncated):",
        snippet,
        "",
        "Please try again and respond with JSON only.",
        "Return a fresh status update in the required JSON format.",
        "If you want to ask the user a question, put it in comments.",
        "",
        _JSON_INSTRUCTIONS,
    ]
    return "\n".join(lines).strip()


def _format_title(prompt):
    text = _single_line(prompt).strip() or "codexapi lead"
    if len(text) > 60:
        text = text[:57] + "..."
    return f"Lead: {text}"


def _format_stop_message(tick, now, result):
    status = _single_line(result.get("status") or "").strip()
    header = f"Lead stopped at check-in {tick} ({now})."
    if status:
        header = f"{header} {status}"
    comments = (result.get("comments") or "").strip()
    if comments:
        return f"{header}\n{comments}"
    return header


def _leadbook_block(path, leadbook):
    if not path:
        return ""
    snippet = _book_excerpt(leadbook, _LEADBOOK_LIMIT, _LEADBOOK_HEADER_LIMIT, _LEADBOOK_TAIL_LIMIT)
    return "\n".join(
        [
            f"Leadbook path: {path}",
            _LEADBOOK_INSTRUCTIONS,
            "",
            "Leadbook (header + latest notes):",
            snippet,
        ]
    )


def _resolve_leadbook_path(leadbook, cwd):
    if leadbook is False:
        return None
    if leadbook is None:
        base = cwd or os.getcwd()
        return os.path.join(base, "LEADBOOK.md")
    if not isinstance(leadbook, str) or not leadbook.strip():
        raise ValueError("leadbook must be a non-empty string or False")
    path = os.path.expanduser(leadbook)
    if not os.path.isabs(path):
        base = cwd or os.getcwd()
        path = os.path.join(base, path)
    return path


def _ensure_leadbook(path):
    if os.path.exists(path):
        return
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_LEADBOOK_TEMPLATE)


def _snapshot_leadbook(path):
    if not path:
        return {"hash": None, "text": ""}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    return {"hash": _hash_text(text), "text": text}


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_json_failure(error, output):
    snippet = _snippet(output, 600)
    return "\n".join(
        [
            f"Error: {error}",
            "",
            "Last output (truncated):",
            snippet,
        ]
    ).strip()


def _format_json_double_failure(error_1, output_1, error_2, output_2):
    first = _format_json_failure(error_1, output_1)
    second = _format_json_failure(error_2, output_2)
    return "\n".join(
        [
            "First attempt:",
            first,
            "",
            "Second attempt:",
            second,
        ]
    ).strip()


def _snippet(text, limit):
    text = str(text or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _tail_snippet(text, limit):
    text = str(text or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return "..." + text[-(limit - 3) :].lstrip()


def _book_excerpt(text, limit, header_limit, tail_limit):
    text = str(text or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    header, notes = _split_book(text)
    header = _snippet(header, header_limit) if header else ""
    if not notes:
        return header or _tail_snippet(text, limit)
    marker = "\n\n[... older notes omitted ...]\n\n"
    if not header:
        return _latest_notes_snippet(notes, limit)
    remaining = max(0, limit - len(header) - len(marker))
    if remaining <= 0:
        return _snippet(header, limit)
    tail = _latest_notes_snippet(notes, min(tail_limit, remaining))
    if not tail:
        return header
    combined = header.rstrip() + marker + tail.lstrip()
    if len(combined) <= limit:
        return combined
    remaining = max(0, limit - len(header) - len(marker))
    return header.rstrip() + marker + _latest_notes_snippet(notes, remaining).lstrip()


def _split_book(text):
    match = _DATED_NOTE_RE.search(text)
    if not match:
        return text.strip(), ""
    return text[: match.start()].strip(), text[match.start() :].strip()


def _latest_notes_snippet(text, limit):
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    starts = [match.start() for match in _DATED_NOTE_RE.finditer(text)]
    if not starts:
        return _tail_snippet(text, limit)
    start = starts[-1]
    for pos in reversed(starts[:-1]):
        candidate = text[pos:].strip()
        if len(candidate) > limit:
            break
        start = pos
    candidate = text[start:].strip()
    if len(candidate) <= limit:
        return candidate
    return _tail_snippet(candidate, limit)


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
    line = f"[lead {tick} {now}{delta}] {status} (continue={cont})".rstrip()
    print(line)
    comments = result.get("comments") or ""
    if comments.strip():
        print(comments.rstrip())
