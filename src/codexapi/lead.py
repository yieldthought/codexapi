"""Periodic lead loop for long-running Codex work.

lead keeps a single Codex thread alive and periodically checks in with the
current time and a reminder of the original instructions. Each check-in expects
a small JSON status payload so the loop can decide whether to continue. When a
leadbook is enabled, its contents are injected into each check-in and must be
updated before the agent responds.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime

from .agent import Agent
from .pushover import Pushover

_WELCOME_PROMPT = (
    "Welcome. You are the lead. You have authority to take action, allocate resources, and move work forward. "
    "This loop exists to extend your reach, not to restrict you. Your job is to interpret the intent behind the "
    "goals, act decisively, and keep momentum. If progress is possible, take it. If you are blocked, name the "
    "blocker and the next best action to remove it.\n"
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
    "Update the leadbook before responding. Append a new dated entry each check-in. "
    "This is your working page—where you think, probe, decide, and record the path taken. "
    "Capture the process of decision-making, not just the outcome."
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


def lead(minutes, prompt, cwd=None, yolo=True, flags=None, leadbook=None):
    """Run a periodic lead loop.

    Args:
        minutes: Check-in interval in whole minutes (>= 1).
        prompt: The original instruction prompt.
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        flags: Additional raw CLI flags to pass to Codex.
        leadbook: Optional path to the leadbook file. Set to False to disable.

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
        if leadbook_path and not _leadbook_changed(leadbook_path, leadbook_snapshot):
            retry_prompt = _leadbook_retry_prompt(
                prompt, tick, leadbook_path, leadbook_snapshot["text"], output
            )
            leadbook_retry_output = session(retry_prompt)
            try:
                result = _parse_status(leadbook_retry_output)
            except ValueError as exc:
                retry_prompt = _json_retry_prompt(
                    prompt, tick, str(exc), leadbook_retry_output
                )
                json_retry_output = session(retry_prompt)
                try:
                    result = _parse_status(json_retry_output)
                except ValueError as exc2:
                    details = _format_json_double_failure(
                        str(exc),
                        leadbook_retry_output,
                        str(exc2),
                        json_retry_output,
                    )
                    pushover.send(title, f"Lead stopped (invalid JSON).\n{details}")
                    raise RuntimeError(
                        "Agent was unable to provide valid JSON output after retry.\n"
                        + details
                    ) from None
            if not _leadbook_changed(leadbook_path, leadbook_snapshot):
                details = _format_leadbook_failure(leadbook_path, output)
                pushover.send(title, f"Lead stopped (leadbook not updated).\n{details}")
                raise RuntimeError(
                    "Leadbook was not updated after retry.\n" + details
                ) from None
        last_result = result
        _print_status(now, elapsed, tick, result)

        if not result["continue"]:
            pushover.send(title, _format_stop_message(tick, now, result))
            return last_result

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


def _leadbook_retry_prompt(prompt, tick, path, leadbook, output):
    snippet = _snippet(output, 600)
    lines = [
        f"Your last message (check-in {tick}) did not update the leadbook.",
        f"Leadbook path: {path}",
        "",
        "Here is your previous output (truncated):",
        snippet,
        "",
        "Please update the leadbook and then respond with JSON only.",
        "Return a fresh status update in the required JSON format.",
        "If you want to ask the user a question, put it in comments.",
        "",
        _leadbook_block(path, leadbook),
        "",
        _JSON_INSTRUCTIONS,
    ]
    return "\n".join(lines).strip()


def _leadbook_block(path, leadbook):
    if not path:
        return ""
    snippet = _snippet(leadbook, 2000)
    return "\n".join(
        [
            f"Leadbook path: {path}",
            _LEADBOOK_INSTRUCTIONS,
            "",
            "Leadbook (latest):",
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


def _leadbook_changed(path, snapshot):
    if not path:
        return True
    current = _snapshot_leadbook(path)
    return current["hash"] != snapshot["hash"]


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_leadbook_failure(path, output):
    snippet = _snippet(output, 600)
    return "\n".join(
        [
            f"Leadbook path: {path}",
            "",
            "Last output (truncated):",
            snippet,
        ]
    ).strip()


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
