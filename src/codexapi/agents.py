"""Durable long-running agent control plane."""

import json
import os
import random
import shlex
import socket
import string
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path

import fcntl

from .agent import Agent
from .pushover import Pushover

_DEFAULT_HOME = "~/.codexapi"
_AGENTBOOK_TEMPLATE = """# Agentbook

Use this file as the durable working memory for the agent.
Append dated notes as work progresses.
Keep entries short and concrete.
"""
_AGENT_PROMPT = (
    "You are a long-term codexapi agent. You are being woken up to make progress "
    "on an ongoing job. Be independent and practical. Manage work and follow "
    "through. Use codexapi task or codexapi science when you want a separate "
    "coding worker. If you need the user's attention, put a short message in the "
    "reply field. If something is urgent and should send Pushover, put it in the "
    "notify field. Respond with JSON only."
)
_AGENT_JSON = (
    "Respond with JSON only (no markdown/backticks/extra text).\n"
    "Return a single JSON object with keys:\n"
    "  status: string (one line)\n"
    "  continue: boolean\n"
    "  reply: string (optional)\n"
    "  notify: string (optional)\n"
)
_COMMAND_KINDS = {"send", "wake", "pause", "resume", "cancel"}
_STOP_POLICIES = {"until_done", "until_stopped"}
_TERMINAL_STATES = {"done", "canceled"}
_ACTIVE_STATES = {"ready", "error", "running", "paused"}


def codexapi_home():
    """Return the resolved codexapi home path."""
    value = os.environ.get("CODEXAPI_HOME", _DEFAULT_HOME)
    return Path(value).expanduser().resolve()


def current_hostname():
    """Return the current hostname."""
    name = socket.gethostname().strip()
    return name or "unknown-host"


def utc_now():
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def format_utc(value):
    """Format a UTC datetime as an ISO string with Z."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def parse_utc(value):
    """Parse a UTC timestamp written by this module."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def start_agent(
    prompt,
    cwd=None,
    name=None,
    created_by=None,
    stop_policy="until_done",
    heartbeat_minutes=5,
    backend=None,
    yolo=True,
    flags=None,
    home=None,
    hostname=None,
    now=None,
):
    """Create a durable agent and return its current snapshot."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if stop_policy not in _STOP_POLICIES:
        raise ValueError("stop_policy must be until_done or until_stopped")
    if heartbeat_minutes < 0:
        raise ValueError("heartbeat_minutes must be >= 0")

    home = _resolve_home(home)
    host = hostname or current_hostname()
    now = now or utc_now()
    _ensure_home(home)

    agent_id = uuid.uuid4().hex
    agent_dir = _agent_dir(home, agent_id)
    commands_new = agent_dir / "commands" / "new"
    commands_claimed = agent_dir / "commands" / "claimed"
    host_dir = agent_dir / "hosts" / host
    runs_dir = host_dir / "runs"

    commands_new.mkdir(parents=True, exist_ok=False)
    commands_claimed.mkdir(parents=True, exist_ok=False)
    runs_dir.mkdir(parents=True, exist_ok=False)

    if created_by is None:
        created_by = os.environ.get("USER") or "user"
    cwd = _resolve_cwd(cwd)
    session = {
        "thread_id": "",
        "backend": backend or os.environ.get("CODEXAPI_BACKEND", "codex"),
        "yolo": bool(yolo),
        "flags": flags or "",
        "cwd": cwd,
        "env": _capture_env(),
        "pending_messages": [],
    }
    agent_name = _choose_name(home, prompt, name)
    meta = {
        "id": agent_id,
        "name": agent_name,
        "created_at": format_utc(now),
        "created_by": str(created_by),
        "hostname": host,
        "cwd": cwd,
        "prompt": prompt.strip(),
        "stop_policy": stop_policy,
        "heartbeat_minutes": int(heartbeat_minutes),
    }
    state = {
        "id": agent_id,
        "name": agent_name,
        "hostname": host,
        "status": "ready",
        "thread_id": "",
        "last_wake_at": "",
        "last_success_at": "",
        "next_wake_at": format_utc(now),
        "wake_requested_at": format_utc(now),
        "unread_message_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "avg_tokens_per_hour": 0.0,
        "child_ids": [],
        "last_error": "",
        "activity": "Created",
        "reply": "",
    }

    _write_json(agent_dir / "meta.json", meta)
    _write_json(agent_dir / "state.json", state)
    _write_json(host_dir / "session.json", session)
    _write_text(agent_dir / "AGENTBOOK.md", _AGENTBOOK_TEMPLATE)
    return _snapshot(agent_dir)


def list_agents(home=None):
    """Return all agents in this CODEXAPI_HOME."""
    home = _resolve_home(home)
    root = home / "agents"
    if not root.exists():
        return []
    agents = []
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        try:
            agents.append(_snapshot(agent_dir))
        except FileNotFoundError:
            continue
    agents.sort(key=lambda item: item["created_at"], reverse=True)
    return agents


def show_agent(agent_ref, home=None):
    """Return a full agent snapshot."""
    agent_dir = resolve_agent_dir(agent_ref, home)
    snapshot = _snapshot(agent_dir)
    snapshot["meta"] = _read_json(agent_dir / "meta.json")
    snapshot["state"] = _read_json(agent_dir / "state.json")
    snapshot["session"] = _read_session(agent_dir)
    snapshot["recent_runs"] = _recent_runs(agent_dir, 5)
    return snapshot


def read_agent(agent_ref, limit=10, home=None):
    """Return recent user-visible communication for an agent."""
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    session = _read_session(agent_dir)
    items = []
    for run in _recent_runs(agent_dir, limit):
        reply = run.get("reply") or ""
        if reply:
            items.append(
                {
                    "kind": "agent",
                    "timestamp": run.get("ended_at") or run.get("started_at") or "",
                    "text": reply,
                }
            )
    for pending in session.get("pending_messages") or []:
        text = pending.get("text") or ""
        if text:
            items.append(
                {
                    "kind": "pending",
                    "timestamp": pending.get("created_at") or "",
                    "text": text,
                }
            )
    items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "status": state.get("status") or "",
        "items": items[:limit],
    }


def send_agent(agent_ref, message, author=None, home=None, hostname=None, now=None):
    """Queue a message for an agent."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    return _queue_command(
        agent_ref,
        "send",
        message.strip(),
        author,
        home,
        hostname,
        now,
    )


def control_agent(agent_ref, kind, author=None, home=None, hostname=None, now=None):
    """Queue a control command for an agent."""
    if kind not in _COMMAND_KINDS - {"send"}:
        raise ValueError(f"Unsupported control command: {kind}")
    return _queue_command(agent_ref, kind, "", author, home, hostname, now)


def tick(home=None, hostname=None, now=None, runner=None):
    """Process due agents for the current host."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    now = now or utc_now()
    _ensure_home(home)
    tick_lock = _tick_lock_path(home, host)
    with _try_lock(tick_lock) as handle:
        if handle is None:
            return {"ran": False, "hostname": host, "processed": 0, "woken": 0}
        _write_lock_info(handle, host, now)
        processed = 0
        woken = 0
        for agent in list_agents(home):
            if agent["hostname"] != host:
                continue
            outcome = _tick_agent(_agent_dir(home, agent["id"]), now, runner)
            if outcome["processed"]:
                processed += 1
            if outcome["woken"]:
                woken += 1
        return {"ran": True, "hostname": host, "processed": processed, "woken": woken}


def install_cron(home=None, hostname=None, python_executable=None, path_value=None):
    """Install or update the cron entry for this home and host."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    _ensure_home(home)
    python_executable = python_executable or sys.executable
    path_value = path_value or os.environ.get("PATH", "")
    wrapper = write_tick_wrapper(home, python_executable, path_value)
    cron_line = render_cron_line(home, host)
    tag = _cron_tag(home, host)
    existing = _read_crontab()
    updated, changed = _upsert_cron_line(existing, cron_line, tag)
    if changed:
        _write_crontab(updated)
    _write_text(home / "cron" / "agent.cron", cron_line + "\n")
    return {
        "hostname": host,
        "home": str(home),
        "wrapper": str(wrapper),
        "cron_line": cron_line,
        "changed": changed,
    }


def resolve_agent_dir(agent_ref, home=None):
    """Resolve an agent by id, unique id prefix, or name."""
    if not isinstance(agent_ref, str) or not agent_ref.strip():
        raise ValueError("agent reference is required")
    home = _resolve_home(home)
    ref = agent_ref.strip()
    matches = []
    for item in list_agents(home):
        if item["id"] == ref:
            return _agent_dir(home, item["id"])
        if item["name"] == ref:
            matches.append(item["id"])
            continue
        if item["id"].startswith(ref):
            matches.append(item["id"])
    matches = sorted(set(matches))
    if not matches:
        raise ValueError(f"Unknown agent: {ref}")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous agent reference: {ref}")
    return _agent_dir(home, matches[0])


def write_tick_wrapper(home=None, python_executable=None, path_value=None):
    """Write the cron wrapper script and return its path."""
    home = _resolve_home(home)
    _ensure_home(home)
    python_executable = python_executable or sys.executable
    path_value = path_value or os.environ.get("PATH", "")
    wrapper = home / "bin" / "agent-tick"
    lines = [
        "#!/bin/bash",
        f"export CODEXAPI_HOME={shlex.quote(str(home))}",
        f"export PATH={shlex.quote(path_value)}",
        f"exec {shlex.quote(str(python_executable))} -m codexapi agent tick",
    ]
    _write_text(wrapper, "\n".join(lines) + "\n")
    wrapper.chmod(0o755)
    return wrapper


def render_cron_line(home=None, hostname=None):
    """Return the cron line for this home and host."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    wrapper = home / "bin" / "agent-tick"
    return f"* * * * * {shlex.quote(str(wrapper))} >/dev/null 2>&1  # { _cron_tag(home, host) }"


def _tick_agent(agent_dir, now, runner):
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    host_dir = agent_dir / "hosts" / meta["hostname"]
    session_path = host_dir / "session.json"
    session = _read_json(session_path)
    run_lock_path = host_dir / "run.lock"

    with _try_lock(run_lock_path) as handle:
        if handle is None:
            return {"processed": False, "woken": False}
        _write_lock_info(handle, meta["hostname"], now)
        changed = False
        if state.get("status") == "running":
            state["status"] = "error"
            state["last_error"] = "Previous wake did not exit cleanly."
            state["activity"] = state["last_error"]
            changed = True
        commands = _claim_commands(agent_dir)
        applied = _apply_commands(meta, state, session, commands, now)
        if applied:
            changed = True
        if changed:
            _sync_state_from_session(state, session)
            _write_json(session_path, session)
            _write_json(agent_dir / "state.json", state)
        if state.get("status") not in ("ready", "error"):
            return {"processed": bool(commands), "woken": False}
        if not _is_due(state, now):
            return {"processed": bool(commands), "woken": False}
        _wake_agent(agent_dir, meta, state, session, now, commands, runner)
        return {"processed": True, "woken": True}


def _wake_agent(agent_dir, meta, state, session, now, commands, runner):
    prompt = _build_wake_prompt(meta, state, session, now, commands, agent_dir)
    state["status"] = "running"
    state["last_wake_at"] = format_utc(now)
    state["wake_requested_at"] = ""
    state["activity"] = "Running"
    _sync_state_from_session(state, session)
    _write_json(agent_dir / "state.json", state)

    run = {
        "id": _run_id(now),
        "started_at": format_utc(now),
        "ended_at": "",
        "wake_reason": _wake_reason(state, commands),
        "commands": [command["kind"] for command in commands],
        "status": "",
        "reply": "",
        "notify": "",
        "error": "",
        "continue": True,
    }
    try:
        outcome = _run_agent_turn(meta, session, prompt, runner)
        response = _parse_agent_response(outcome["message"])
        ended = utc_now()
        session["thread_id"] = outcome.get("thread_id") or session.get("thread_id") or ""
        session["pending_messages"] = []
        state["reply"] = response["reply"]
        state["last_success_at"] = format_utc(ended)
        state["last_error"] = ""
        state["thread_id"] = session["thread_id"]
        state["wake_requested_at"] = ""
        state["activity"] = response["status"]
        if response["continue"]:
            state["status"] = "ready"
            state["next_wake_at"] = format_utc(
                ended + timedelta(minutes=meta["heartbeat_minutes"])
            )
        else:
            state["status"] = "done"
            state["next_wake_at"] = ""
        _sync_state_from_session(state, session)
        _write_json(agent_dir / "hosts" / meta["hostname"] / "session.json", session)
        _write_json(agent_dir / "state.json", state)
        run["ended_at"] = format_utc(ended)
        run["status"] = response["status"]
        run["reply"] = response["reply"]
        run["notify"] = response["notify"]
        run["continue"] = bool(response["continue"])
        _write_run(agent_dir, meta["hostname"], run)
        if response["notify"]:
            title = f"Agent: {meta['name']}"
            Pushover().send(title, response["notify"])
    except Exception as exc:
        ended = utc_now()
        state["status"] = "error"
        state["last_error"] = _single_line(str(exc)) or exc.__class__.__name__
        state["activity"] = state["last_error"]
        state["wake_requested_at"] = ""
        state["next_wake_at"] = format_utc(
            ended + timedelta(minutes=meta["heartbeat_minutes"])
        )
        _sync_state_from_session(state, session)
        _write_json(agent_dir / "hosts" / meta["hostname"] / "session.json", session)
        _write_json(agent_dir / "state.json", state)
        run["ended_at"] = format_utc(ended)
        run["error"] = state["last_error"]
        _write_run(agent_dir, meta["hostname"], run)


def _run_agent_turn(meta, session, prompt, runner=None):
    if runner is not None:
        return runner(meta, session, prompt)
    worker = Agent(
        session.get("cwd") or meta.get("cwd"),
        session.get("yolo", True),
        session.get("thread_id") or None,
        session.get("flags") or None,
        include_thinking=False,
        backend=session.get("backend") or None,
        env=session.get("env") or None,
    )
    message = worker(prompt)
    return {"message": message, "thread_id": worker.thread_id or ""}


def _parse_agent_response(output):
    text = _strip_fence(str(output or "").strip())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON response: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("Agent response must be a JSON object.")
    status = payload.get("status")
    cont = payload.get("continue")
    reply = payload.get("reply")
    notify = payload.get("notify")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Agent response missing string 'status'.")
    if not isinstance(cont, bool):
        raise ValueError("Agent response missing boolean 'continue'.")
    if reply is None:
        reply = ""
    if notify is None:
        notify = ""
    if not isinstance(reply, str):
        raise ValueError("Agent response missing string 'reply'.")
    if not isinstance(notify, str):
        raise ValueError("Agent response missing string 'notify'.")
    return {
        "status": _single_line(status),
        "continue": cont,
        "reply": reply.strip(),
        "notify": notify.strip(),
    }


def _build_wake_prompt(meta, state, session, now, commands, agent_dir):
    messages = session.get("pending_messages") or []
    lines = [
        _AGENT_PROMPT,
        "",
        f"Current UTC time: {format_utc(now)}",
        f"Agent name: {meta['name']}",
        f"Stop policy: {meta['stop_policy']}",
        f"Heartbeat minutes: {meta['heartbeat_minutes']}",
        "",
        "Original instructions:",
        meta["prompt"],
        "",
        f"Working directory: {meta['cwd']}",
        f"Agentbook path: {agent_dir / 'AGENTBOOK.md'}",
        "Append a dated note to the agentbook before you respond.",
    ]
    book = _read_text(agent_dir / "AGENTBOOK.md")
    if book.strip():
        lines.extend(["", "Agentbook (latest):", _snippet(book, 3000)])
    if messages:
        lines.extend(["", "Queued user messages:"])
        for message in messages:
            created_at = message.get("created_at") or ""
            author = message.get("author") or "user"
            text = message.get("text") or ""
            lines.append(f"- [{created_at}] {author}: {text}")
    else:
        lines.extend(["", "Queued user messages: none."])
    if commands:
        lines.extend(["", "Wake triggers:"])
        for command in commands:
            lines.append(f"- {command['kind']}")
    last_reply = state.get("reply") or ""
    if last_reply:
        lines.extend(["", "Your last visible reply:", _snippet(last_reply, 1200)])
    lines.extend(["", _AGENT_JSON])
    return "\n".join(lines).strip()


def _claim_commands(agent_dir):
    new_dir = agent_dir / "commands" / "new"
    claimed_dir = agent_dir / "commands" / "claimed"
    commands = []
    for path in sorted(new_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        target = claimed_dir / path.name
        try:
            path.rename(target)
        except FileNotFoundError:
            continue
        command = _read_json(target)
        command["_path"] = str(target)
        commands.append(command)
    return commands


def _apply_commands(meta, state, session, commands, now):
    changed = False
    pending = list(session.get("pending_messages") or [])
    for command in commands:
        kind = command.get("kind")
        if kind == "send":
            pending.append(
                {
                    "id": command.get("id") or "",
                    "created_at": command.get("created_at") or format_utc(now),
                    "author": command.get("author") or "user",
                    "origin_hostname": command.get("origin_hostname") or "",
                    "text": command.get("body") or "",
                }
            )
            state["wake_requested_at"] = format_utc(now)
            changed = True
        elif kind == "wake":
            state["wake_requested_at"] = format_utc(now)
            changed = True
        elif kind == "pause":
            state["status"] = "paused"
            state["activity"] = "Paused"
            changed = True
        elif kind == "resume":
            if state.get("status") == "paused":
                state["status"] = "ready"
            state["wake_requested_at"] = format_utc(now)
            state["activity"] = "Resumed"
            changed = True
        elif kind == "cancel":
            state["status"] = "canceled"
            state["activity"] = "Canceled"
            state["wake_requested_at"] = ""
            state["next_wake_at"] = ""
            changed = True
    session["pending_messages"] = pending
    _sync_state_from_session(state, session)
    for command in commands:
        path = command.get("_path")
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return changed


def _is_due(state, now):
    status = state.get("status")
    if status not in ("ready", "error"):
        return False
    if state.get("wake_requested_at"):
        return True
    if status == "ready" and int(state.get("unread_message_count") or 0) > 0:
        return True
    next_wake = parse_utc(state.get("next_wake_at"))
    if next_wake and next_wake <= now:
        return True
    return False


def _write_run(agent_dir, hostname, payload):
    runs_dir = agent_dir / "hosts" / hostname / "runs"
    filename = f"{payload['id']}.json"
    _write_json(runs_dir / filename, payload)


def _recent_runs(agent_dir, limit):
    meta = _read_json(agent_dir / "meta.json")
    runs_dir = agent_dir / "hosts" / meta["hostname"] / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for path in sorted(runs_dir.iterdir(), key=lambda item: item.name, reverse=True):
        if not path.is_file() or path.suffix != ".json":
            continue
        runs.append(_read_json(path))
        if len(runs) >= limit:
            break
    return runs


def _queue_command(agent_ref, kind, body, author, home, hostname, now):
    if kind not in _COMMAND_KINDS:
        raise ValueError(f"Unsupported command: {kind}")
    agent_dir = resolve_agent_dir(agent_ref, home)
    now = now or utc_now()
    host = hostname or current_hostname()
    author = author or os.environ.get("USER") or "user"
    payload = {
        "id": _command_id(now, host),
        "created_at": format_utc(now),
        "origin_hostname": host,
        "kind": kind,
        "body": body,
        "author": str(author),
    }
    new_dir = agent_dir / "commands" / "new"
    _atomic_create_json(new_dir, f"{payload['id']}.json", payload)
    return payload


def _snapshot(agent_dir):
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    return {
        "id": meta["id"],
        "name": meta["name"],
        "created_at": meta["created_at"],
        "created_by": meta["created_by"],
        "hostname": meta["hostname"],
        "cwd": meta["cwd"],
        "stop_policy": meta["stop_policy"],
        "heartbeat_minutes": meta["heartbeat_minutes"],
        "status": state.get("status") or "",
        "thread_id": state.get("thread_id") or "",
        "last_wake_at": state.get("last_wake_at") or "",
        "last_success_at": state.get("last_success_at") or "",
        "next_wake_at": state.get("next_wake_at") or "",
        "wake_requested_at": state.get("wake_requested_at") or "",
        "unread_message_count": int(state.get("unread_message_count") or 0),
        "input_tokens": int(state.get("input_tokens") or 0),
        "output_tokens": int(state.get("output_tokens") or 0),
        "total_tokens": int(state.get("total_tokens") or 0),
        "avg_tokens_per_hour": float(state.get("avg_tokens_per_hour") or 0.0),
        "last_error": state.get("last_error") or "",
        "activity": state.get("activity") or "",
        "reply": state.get("reply") or "",
    }


def _choose_name(home, prompt, requested):
    base = _slugify(requested or prompt)
    if not base:
        base = "agent"
    existing = {item["name"] for item in list_agents(home)}
    if base not in existing:
        return base
    index = 2
    while True:
        candidate = f"{base}-{index}"
        if candidate not in existing:
            return candidate
        index += 1


def _slugify(text):
    if not isinstance(text, str):
        return ""
    cleaned = []
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
            continue
        cleaned.append("-")
    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    if not slug:
        return ""
    parts = [part for part in slug.split("-") if part]
    if not parts:
        return ""
    return "-".join(parts[:6])


def _resolve_cwd(cwd):
    target = cwd or os.getcwd()
    return str(Path(target).expanduser().resolve())


def _capture_env():
    env = {}
    for key in ("PATH", "VIRTUAL_ENV"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _resolve_home(home):
    if home is None:
        return codexapi_home()
    return Path(home).expanduser().resolve()


def _ensure_home(home):
    (home / "agents").mkdir(parents=True, exist_ok=True)
    (home / "locks").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    (home / "cron").mkdir(parents=True, exist_ok=True)


def _agent_dir(home, agent_id):
    return home / "agents" / agent_id


def _tick_lock_path(home, hostname):
    return home / "locks" / f".tick.{hostname}.lock"


@contextmanager
def _try_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield handle
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_lock_info(handle, hostname, now):
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "hostname": hostname, "started_at": format_utc(now)}))
    handle.flush()


def _read_session(agent_dir):
    meta = _read_json(agent_dir / "meta.json")
    return _read_json(agent_dir / "hosts" / meta["hostname"] / "session.json")


def _sync_state_from_session(state, session):
    pending = session.get("pending_messages") or []
    state["unread_message_count"] = len(pending)
    state["thread_id"] = session.get("thread_id") or ""


def _command_id(now, hostname):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    rand = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{stamp}.{hostname}.{os.getpid()}.{rand}"


def _run_id(now):
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _atomic_create_json(directory, filename, payload):
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, directory / filename)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix or ".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix or ".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def _snippet(text, limit):
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _strip_fence(text):
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 3:
        return text
    if lines[-1].strip() != "```":
        return text
    return "\n".join(lines[1:-1]).strip()


def _single_line(text):
    if not text:
        return ""
    return " ".join(str(text).replace("\r", " ").split())


def _wake_reason(state, commands):
    reasons = []
    if state.get("wake_requested_at"):
        reasons.append("wake_requested")
    if int(state.get("unread_message_count") or 0) > 0:
        reasons.append("messages")
    if commands:
        reasons.append("commands")
    if not reasons:
        reasons.append("heartbeat")
    return ",".join(sorted(set(reasons)))


def _cron_tag(home, hostname):
    key = sha1(str(home).encode("utf-8")).hexdigest()[:12]
    return f"codexapi-agent::{hostname}::{key}"


def _upsert_cron_line(existing, line, tag):
    lines = []
    found = False
    for raw in str(existing or "").splitlines():
        if raw.strip().endswith(f"# {tag}"):
            if not found:
                lines.append(line)
                found = True
            continue
        lines.append(raw)
    if not found:
        lines.append(line)
        found = True
        changed = True
    else:
        changed = "\n".join(lines).strip() != str(existing or "").strip()
    text = "\n".join(item for item in lines if item is not None)
    if text and not text.endswith("\n"):
        text += "\n"
    return text, changed


def _read_crontab():
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout
    stderr = (result.stderr or "").strip().lower()
    stdout = (result.stdout or "").strip().lower()
    if "no crontab" in stderr or "no crontab" in stdout:
        return ""
    raise RuntimeError(result.stderr.strip() or "crontab -l failed")


def _write_crontab(text):
    result = subprocess.run(
        ["crontab", "-"],
        input=text,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "crontab install failed")
