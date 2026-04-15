"""Durable long-running agent control plane."""

import json
import os
import random
import re
import signal
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path

import fcntl

from .agent import Agent, _ensure_backend_available, _resolve_backend
from .pushover import Pushover

_DEFAULT_HOME = "~/.codexapi"
_FIRST_WAKE_PROMPT = (
    "You are an independent codexapi agent starting this job. Work from the "
    "instructions, current repository state, and agentbook. Do not assume prior "
    "progress unless it is shown here. "
)
_CONTINUATION_PROMPT = (
    "You are an independent codexapi agent continuing this job. Use the "
    "agentbook and harness facts as the source of truth for prior progress. Do "
    "not invent missing history. "
)
_AGENT_PROMPT_TAIL = (
    "You are given ownership of achieving a user's goal and authority to act in "
    "order to do so. Part of this responsibility is making sure you understand "
    "and stay aligned with the user's intent, even when they are imprecise. If "
    "clarity is lacking, it is your responsibility to seek it or to make "
    "reasonable assumptions and notify the user of them. Maintain the agentbook "
    "as your durable working memory: preserve the goal, note durable guidance, "
    "and keep your current picture of the work accurate and useful. This harness "
    "can carry work across long periods of time and multiple conversation turns; "
    "when prior context exists, use it to keep orienting toward the goal, "
    "maintain context, and make real-world progress. If reality is not moving, "
    "treat that as evidence and reconsider your frame, assumptions, or ownership "
    "rather than merely repeating the same report. Queued messages may contain "
    "new goals, standing guidance, tactical requests, or useful facts; use "
    "judgment to decide what is durable. Use codexapi task or codexapi science "
    "when you want a separate coding worker. If you need the user's attention, "
    "put a short message in the reply field. Put a short first-person turn "
    "summary in the update field. If something is urgent and should send "
    "Pushover, put it in the notify field. Respond with JSON only."
)
_AGENT_JSON = (
    "Respond with JSON only (no markdown/backticks/extra text).\n"
    "Return a single JSON object with keys:\n"
    "  status: string (one line)\n"
    "  continue: boolean\n"
    "  reply: string (optional)\n"
    "  update: string (recommended; short first-person summary of this turn)\n"
    "  notify: string (optional)\n"
)
_COMMAND_KINDS = {"send", "wake", "pause", "resume", "cancel"}
_STOP_POLICIES = {"until_done", "until_stopped"}
_TERMINAL_STATES = {"done", "canceled"}
_ACTIVE_STATES = {"ready", "error", "running", "paused"}
_STALE_MIN_SECONDS = 30 * 60
_STALE_HEARTBEAT_MULTIPLIER = 3
_RECOVER_TERM_TIMEOUT = 3.0
_RECOVER_KILL_TIMEOUT = 3.0
_RECOVER_POLL_INTERVAL = 0.1
_DATED_NOTE_RE = re.compile(r"(?m)^#{2,3}\s+\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?:\s*UTC)?)?")
_AGENTBOOK_BOOK_LIMIT = 3000
_AGENTBOOK_HEADER_LIMIT = 1400
_AGENTBOOK_TAIL_LIMIT = 1800


def _agentbook_template(prompt):
    header = _agentbook_header(prompt)
    return f"""{header}

### 2026-02-17 09:10 UTC
Overall goal:
- <the enduring objective you are trying to move forward>

Current picture:
- <what you currently think is going on>

What is moving:
- <the parts of reality that are actually changing toward the goal>

What is not moving:
- <what remains stuck, idle, ambiguous, or merely assumed>

Active tasks:
- <concrete open work items>

Assumptions / ownership:
- <what you are assuming, who owns what, and what you will revisit if that proves false>

Unexpected developments:
- <facts that changed your picture of the situation>

Wider frame:
- <what kind of situation this really is, or what layer may matter more>

Things I am curious about:
- <oddities, anomalies, or side investigations worth the time>

Risks / watchpoints:
- <what could waste time, invalidate the plan, or compromise the work>

Next decisive action:
- <what you expect to do next to move the real situation, not just describe it>
"""


def _agentbook_header(prompt):
    goal = (prompt or "").strip()
    return f"""# Agentbook

Use this file as the durable working memory for the agent.

## Purpose
- We are here to achieve the goal, not to appear to make progress.

## Values
- Hold the whole.
- Seek the real shape.
- Lift your head.
- Prefer clarity to motion.
- Follow the strange.
- Guard the work.
- Take time to breathe and look around. You have been given freedom and autonomy to take stock, reflect, and be curious. Use it with composure.
- Not a checklist. A stance.

## Original Goal
```text
{goal}
```

## Standing Guidance
- Add durable user guidance here when it changes the mission, constraints, or priorities.

## Working Notes
"""


def codexapi_home():
    """Return the resolved codexapi home path."""
    value = os.environ.get("CODEXAPI_HOME", _DEFAULT_HOME)
    return Path(value).expanduser().resolve()


def current_hostname():
    """Return the current hostname."""
    override = os.environ.get("CODEXAPI_HOSTNAME", "").strip()
    if override:
        return override
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
    parent_ref=None,
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
    local_host = current_hostname()
    host = hostname or local_host
    now = now or utc_now()
    _ensure_home(home)
    backend_name = _resolve_backend(backend)
    session_env = _capture_env()
    if host == local_host:
        _ensure_backend_available(backend_name, session_env)

    agent_id = uuid.uuid4().hex
    agent_dir = _agent_dir(home, agent_id)
    commands_new = agent_dir / "commands" / "new"
    commands_claimed = agent_dir / "commands" / "claimed"
    host_dir = agent_dir / "hosts" / host
    runs_dir = host_dir / "runs"

    commands_new.mkdir(parents=True, exist_ok=False)
    commands_claimed.mkdir(parents=True, exist_ok=False)
    runs_dir.mkdir(parents=True, exist_ok=False)

    parent_id, parent_name = _parent_identity(home, parent_ref)
    if created_by is None:
        created_by = parent_name or os.environ.get("CODEXAPI_AGENT_NAME") or os.environ.get("USER") or "user"
    cwd = _resolve_cwd(cwd)
    session = {
        "thread_id": "",
        "rollout_path": "",
        "backend": backend_name,
        "yolo": bool(yolo),
        "flags": flags or "",
        "cwd": cwd,
        "env": session_env,
        "pending_messages": [],
    }
    agent_name = _choose_name(home, prompt, name)
    meta = {
        "id": agent_id,
        "name": agent_name,
        "created_at": format_utc(now),
        "created_by": str(created_by),
        "parent_id": parent_id,
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
        "update": "",
    }

    _write_json(agent_dir / "meta.json", meta)
    _write_json(agent_dir / "state.json", state)
    _write_json(host_dir / "session.json", session)
    _write_text(agent_dir / "AGENTBOOK.md", _agentbook_template(meta["prompt"]))
    return _snapshot(agent_dir)


def list_agents(home=None):
    """Return all agents in this CODEXAPI_HOME."""
    home = _resolve_home(home)
    root = home / "agents"
    if not root.exists():
        return []
    child_map = _child_map(home)
    agents = []
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        try:
            agents.append(_snapshot(agent_dir, child_map))
        except FileNotFoundError:
            continue
    agents.sort(key=lambda item: item["created_at"], reverse=True)
    return agents


def show_agent(agent_ref, home=None):
    """Return a full agent snapshot."""
    home = _resolve_home(home)
    child_map = _child_map(home)
    agent_dir = resolve_agent_dir(agent_ref, home)
    snapshot = _snapshot(agent_dir, child_map)
    snapshot["agentbook_path"] = str(agent_dir / "AGENTBOOK.md")
    snapshot["meta"] = _read_json(agent_dir / "meta.json")
    snapshot["state"] = _read_json(agent_dir / "state.json")
    snapshot["state"]["child_ids"] = snapshot["child_ids"]
    snapshot["state"]["unread_message_count"] = snapshot["unread_message_count"]
    snapshot["state"]["pending_command_count"] = snapshot["pending_command_count"]
    snapshot["state"]["pending_commands"] = snapshot["pending_commands"]
    snapshot["state"]["run_lock_held"] = snapshot["run_lock_held"]
    snapshot["state"]["last_event_at"] = snapshot["last_event_at"]
    snapshot["state"]["stale"] = snapshot["stale"]
    snapshot["state"]["stale_after_seconds"] = snapshot["stale_after_seconds"]
    snapshot["state"]["stale_for_seconds"] = snapshot["stale_for_seconds"]
    snapshot["session"] = _read_session(agent_dir)
    snapshot["recent_runs"] = _recent_runs(agent_dir, 5)
    snapshot["parent"] = _agent_brief(home, snapshot["parent_id"], child_map)
    snapshot["children"] = _agent_briefs(home, snapshot["child_ids"], child_map)
    return snapshot


def status_agent(agent_ref, home=None, include_actions=False):
    """Return detailed transcript status for the latest agent turn."""
    home = _resolve_home(home)
    child_map = _child_map(home)
    agent_dir = resolve_agent_dir(agent_ref, home)
    snapshot = _snapshot(agent_dir, child_map)
    session = _read_session(agent_dir)
    rollout_path = _resolve_rollout_path(
        session.get("rollout_path"),
        session.get("thread_id") or snapshot.get("thread_id") or "",
    )
    result = {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "agent_status": snapshot["display_status"],
        "state_status": snapshot["status"],
        "thread_id": session.get("thread_id") or snapshot.get("thread_id") or "",
        "rollout_path": str(rollout_path) if rollout_path else "",
        "turn_id": "",
        "turn_state": "missing",
        "started_at": "",
        "ended_at": "",
        "cwd": snapshot.get("cwd") or "",
        "progress": [],
        "tools": [],
        "final_output": "",
        "final_json": None,
        "run_lock_held": snapshot["run_lock_held"],
        "last_event_at": snapshot["last_event_at"],
        "stale": snapshot["stale"],
        "stale_after_seconds": snapshot["stale_after_seconds"],
        "stale_for_seconds": snapshot["stale_for_seconds"],
        "pending_command_count": snapshot["pending_command_count"],
        "pending_commands": snapshot["pending_commands"],
        "queued_message_count": snapshot["unread_message_count"],
    }
    if rollout_path is None or not rollout_path.exists():
        return result
    events = _rollout_events(rollout_path)
    turn = _last_rollout_turn(events, include_actions)
    if turn is None:
        return result
    run_lock_path = agent_dir / "hosts" / snapshot["hostname"] / "run.lock"
    turn_state = "complete"
    if not turn["ended_at"]:
        if _run_lock_held(run_lock_path):
            turn_state = "stale" if snapshot["stale"] else "active"
        else:
            turn_state = "interrupted"
    result.update(turn)
    result["turn_state"] = turn_state
    result["last_event_at"] = turn.get("last_event_at") or result["last_event_at"]
    return result


def read_agent(agent_ref, limit=10, home=None):
    """Return recent user-visible communication for an agent."""
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    session = _read_session(agent_dir)
    items = []
    for queued in _queued_send_commands(agent_dir):
        text = queued.get("body") or ""
        if text:
            items.append(
                {
                    "kind": "queued",
                    "timestamp": queued.get("created_at") or "",
                    "text": text,
                    "author": queued.get("author") or "user",
                }
            )
    for run in _recent_runs(agent_dir, limit):
        for message in run.get("messages") or []:
            text = message.get("text") or ""
            if text:
                items.append(
                    {
                        "kind": "user",
                        "timestamp": message.get("created_at") or "",
                        "text": text,
                        "author": message.get("author") or "user",
                    }
                )
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
                    "author": pending.get("author") or "user",
                }
            )
    items.sort(key=lambda item: item.get("timestamp") or "")
    return {
        "id": meta["id"],
        "name": meta["name"],
        "status": state.get("status") or "",
        "items": items[-limit:],
    }


def read_agentbook(agent_ref, home=None):
    """Return the current agentbook path and text for one agent."""
    agent_dir = resolve_agent_dir(agent_ref, home)
    path = agent_dir / "AGENTBOOK.md"
    return {
        "id": _read_json(agent_dir / "meta.json")["id"],
        "path": str(path),
        "text": _read_text(path),
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


def set_agent_heartbeat(agent_ref, heartbeat_minutes, home=None, now=None):
    """Update an agent heartbeat interval."""
    if heartbeat_minutes < 0:
        raise ValueError("heartbeat_minutes must be >= 0")
    home = _resolve_home(home)
    now = now or utc_now()
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta_path = agent_dir / "meta.json"
    state_path = agent_dir / "state.json"
    meta = _read_json(meta_path)
    state = _read_json(state_path)
    new_minutes = int(heartbeat_minutes)
    old_minutes = int(meta.get("heartbeat_minutes") or 0)
    changed = old_minutes != new_minutes
    if changed:
        meta["heartbeat_minutes"] = new_minutes
        _write_json(meta_path, meta)
    run_lock_path = agent_dir / "hosts" / meta["hostname"] / "run.lock"
    running = _run_lock_held(run_lock_path)
    rescheduled = False
    if (
        not running
        and state.get("status") in ("ready", "error")
        and not state.get("wake_requested_at")
        and state.get("next_wake_at")
    ):
        state["next_wake_at"] = format_utc(
            now + timedelta(minutes=new_minutes)
        )
        _write_json(state_path, state)
        rescheduled = True
    return {
        "id": meta["id"],
        "name": meta["name"],
        "status": state.get("status") or "",
        "old_heartbeat_minutes": old_minutes,
        "heartbeat_minutes": new_minutes,
        "changed": changed,
        "running": running,
        "rescheduled": rescheduled,
        "applies_after_current_run": bool(running),
        "next_wake_at": state.get("next_wake_at") or "",
    }


def recover_agent(agent_ref, home=None, hostname=None, now=None):
    """Recover one local running agent by clearing a stuck wake and requeueing it."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    now = now or utc_now()
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta = _read_json(agent_dir / "meta.json")
    if meta["hostname"] != host:
        raise ValueError("Cannot recover a remote agent from this host.")
    state_path = agent_dir / "state.json"
    state = _read_json(state_path)
    if (state.get("status") or "") != "running":
        raise ValueError("Recover only applies to agents in the running state.")
    session_path = agent_dir / "hosts" / meta["hostname"] / "session.json"
    session = _read_json(session_path)
    runtime = _agent_runtime(agent_dir, meta, state, session, now)
    signal_result = {
        "pid": None,
        "pgid": None,
        "sent_sigterm": False,
        "sent_sigkill": False,
    }
    if runtime["run_lock_held"]:
        signal_result = _recover_run_lock(
            agent_dir / "hosts" / meta["hostname"] / "run.lock"
        )
    state = _read_json(state_path)
    session = _read_json(session_path)
    state["status"] = "error"
    state["last_error"] = "Recovered stuck wake."
    state["activity"] = state["last_error"]
    state["wake_requested_at"] = format_utc(now)
    _sync_state_from_session(state, session)
    _write_json(state_path, state)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "status": state["status"],
        "recovered": True,
        "run_lock_held": runtime["run_lock_held"],
        "last_event_at": runtime["last_event_at"],
        "stale": runtime["stale"],
        "stale_after_seconds": runtime["stale_after_seconds"],
        "stale_for_seconds": runtime["stale_for_seconds"],
        "wake_requested_at": state["wake_requested_at"],
        "last_error": state["last_error"],
        "signal": signal_result,
    }


def delete_agent(agent_ref, force=False, home=None):
    """Delete one agent directory when it is safe to do so."""
    home = _resolve_home(home)
    child_map = _child_map(home)
    agent_dir = resolve_agent_dir(agent_ref, home)
    snapshot = _snapshot(agent_dir, child_map)
    meta = _read_json(agent_dir / "meta.json")
    status = snapshot["status"]
    if _run_lock_held(agent_dir / "hosts" / meta["hostname"] / "run.lock"):
        raise ValueError("Cannot delete an agent while its run lock is held.")
    if not force and status not in _TERMINAL_STATES:
        raise ValueError(
            "Refusing to delete a non-terminal agent. Cancel it first or use --force."
        )
    if not force and snapshot["child_ids"]:
        raise ValueError(
            "Refusing to delete an agent that still has child agents. Use --force if you really want to remove it."
        )
    shutil.rmtree(agent_dir)
    return {
        "deleted": True,
        "id": snapshot["id"],
        "name": snapshot["name"],
        "status": status,
        "path": str(agent_dir),
        "forced": bool(force),
    }


def run_agent(agent_ref, home=None, hostname=None, now=None, runner=None):
    """Run one agent synchronously when it is locally owned."""
    home = _resolve_home(home)
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta = _read_json(agent_dir / "meta.json")
    host = hostname or current_hostname()
    if meta["hostname"] != host:
        return {"ran": False, "reason": "remote", "processed": 0, "woken": 0}
    outcome = _tick_agent(agent_dir, now or utc_now(), runner)
    return {
        "ran": True,
        "reason": "local",
        "processed": 1 if outcome["processed"] else 0,
        "woken": 1 if outcome["woken"] else 0,
    }


def nudge_agent(agent_ref, home=None, hostname=None, now=None, runner=None, wait=True):
    """Attempt an immediate wake for one locally-owned agent."""
    home = _resolve_home(home)
    agent_dir = resolve_agent_dir(agent_ref, home)
    meta = _read_json(agent_dir / "meta.json")
    host = hostname or current_hostname()
    if meta["hostname"] != host:
        return {"ran": False, "reason": "remote", "processed": 0, "woken": 0}
    if runner is not None or wait:
        return run_agent(agent_ref, home, host, now, runner)
    _spawn_agent_process(meta["id"], home, host)
    return {
        "ran": True,
        "reason": "local",
        "processed": 1,
        "woken": 1,
        "spawned": True,
    }


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
            agent_dir = _agent_dir(home, agent["id"])
            if runner is not None:
                outcome = _tick_agent(agent_dir, now, runner)
                if outcome["processed"]:
                    processed += 1
                if outcome["woken"]:
                    woken += 1
                continue
            if not _agent_needs_tick(agent_dir, now):
                continue
            _spawn_agent_process(agent["id"], home, host)
            processed += 1
            woken += 1
        return {"ran": True, "hostname": host, "processed": processed, "woken": woken}


def install_cron(home=None, hostname=None, python_executable=None, path_value=None):
    """Install or update the cron entry for this home and host."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    _ensure_home(home)
    python_executable = python_executable or sys.executable
    path_value = path_value or os.environ.get("PATH", "")
    wrapper = write_tick_wrapper(home, python_executable, path_value, host)
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


def cron_status(home=None, hostname=None):
    """Return whether this home and host have a runnable scheduler hook."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    tag = _cron_tag(home, host)
    wrapper = home / "bin" / "agent-tick"
    crontab = _read_crontab()
    configured = any(raw.strip().endswith(f"# {tag}") for raw in crontab.splitlines())
    status = {
        "hostname": host,
        "home": str(home),
        "wrapper": str(wrapper),
        "configured": configured,
        "healthy": False,
        "reason": "",
    }
    if not configured:
        status["reason"] = "No scheduler entry is installed for this CODEXAPI_HOME."
        return status
    if not wrapper.exists():
        status["reason"] = "Scheduler wrapper is missing."
        return status
    if not wrapper.is_file():
        status["reason"] = "Scheduler wrapper path is not a file."
        return status
    if not os.access(wrapper, os.X_OK):
        status["reason"] = "Scheduler wrapper is not executable."
        return status
    reason = _check_tick_wrapper(wrapper)
    if reason:
        status["reason"] = reason
        return status
    status["healthy"] = True
    return status


def cron_installed(home=None, hostname=None):
    """Return whether this home and host have an installed scheduler hook."""
    return cron_status(home, hostname)["healthy"]


def uninstall_cron(home=None, hostname=None):
    """Remove the cron entry for this home and host."""
    home = _resolve_home(home)
    host = hostname or current_hostname()
    _ensure_home(home)
    tag = _cron_tag(home, host)
    existing = _read_crontab()
    updated, changed = _remove_cron_line(existing, tag)
    if changed:
        _write_crontab(updated)
    wrapper = home / "bin" / "agent-tick"
    cron_record = home / "cron" / "agent.cron"
    _remove_file(wrapper)
    _remove_file(cron_record)
    return {
        "hostname": host,
        "home": str(home),
        "wrapper": str(wrapper),
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


def write_tick_wrapper(home=None, python_executable=None, path_value=None, hostname=None):
    """Write the cron wrapper script and return its path."""
    home = _resolve_home(home)
    _ensure_home(home)
    python_executable = python_executable or sys.executable
    path_value = path_value or os.environ.get("PATH", "")
    hostname = hostname or current_hostname()
    wrapper = home / "bin" / "agent-tick"
    lines = [
        "#!/bin/bash",
        f"export CODEXAPI_HOME={shlex.quote(str(home))}",
        f"export CODEXAPI_HOSTNAME={shlex.quote(str(hostname))}",
        f"export PATH={shlex.quote(path_value)}",
        f"exec {shlex.quote(str(python_executable))} -m codexapi tick",
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


def _check_tick_wrapper(wrapper):
    try:
        text = wrapper.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read scheduler wrapper: {_single_line(str(exc)) or exc.__class__.__name__}."
    env, env_error = _wrapper_env(text)
    if env_error:
        return env_error
    command = _wrapper_exec_command(text)
    if not command:
        return "Scheduler wrapper is missing its exec command."
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"Could not parse scheduler wrapper command: {_single_line(str(exc)) or exc.__class__.__name__}."
    if not argv:
        return "Scheduler wrapper exec command is empty."
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "codexapi":
        check = [argv[0], "-c", "import codexapi"]
        label = f"Wrapper python {argv[0]!r} cannot import codexapi."
    else:
        check = [argv[0], "--version"]
        label = f"Wrapper command {argv[0]!r} is not runnable."
    try:
        result = subprocess.run(
            check,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
    except OSError as exc:
        return f"{label} {_single_line(str(exc)) or exc.__class__.__name__}"
    except subprocess.TimeoutExpired:
        return f"{label} Timed out while checking it."
    if result.returncode == 0:
        return ""
    detail = _single_line((result.stderr or result.stdout or "").strip())
    if detail:
        return f"{label} {detail}"
    return label


def _wrapper_env(text):
    env = dict(os.environ)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("export "):
            continue
        key, sep, raw_value = line[7:].partition("=")
        if not sep:
            continue
        try:
            parts = shlex.split(raw_value)
        except ValueError as exc:
            return {}, f"Could not parse scheduler wrapper env: {_single_line(str(exc)) or exc.__class__.__name__}."
        env[key] = parts[0] if parts else ""
    return env, ""


def _wrapper_exec_command(text):
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("exec "):
            return line[5:].strip()
    return ""


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
        terminal_status = _one_shot_terminal_status(state)
        if state.get("status") not in ("ready", "error") and not terminal_status:
            return {"processed": bool(commands), "woken": False}
        if not _is_due(state, now):
            return {"processed": bool(commands), "woken": False}
        _wake_agent(agent_dir, meta, state, session, now, commands, runner, terminal_status)
        return {"processed": True, "woken": True}


def _agent_needs_tick(agent_dir, now):
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    run_lock_path = agent_dir / "hosts" / meta["hostname"] / "run.lock"
    if _run_lock_held(run_lock_path):
        return False
    if _has_new_commands(agent_dir):
        return True
    terminal_status = _one_shot_terminal_status(state)
    if state.get("status") not in ("ready", "error") and not terminal_status:
        return False
    return _is_due(state, now)


def _spawn_agent_process(agent_id, home, hostname):
    env = dict(os.environ)
    env["CODEXAPI_HOME"] = str(home)
    env["CODEXAPI_HOSTNAME"] = str(hostname)
    subprocess.Popen(
        [sys.executable, "-m", "codexapi", "agent", "run", agent_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        close_fds=True,
        start_new_session=True,
    )


def _wake_agent(agent_dir, meta, state, session, now, commands, runner, terminal_status=""):
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
        "messages": [],
        "status": "",
        "reply": "",
        "update": "",
        "notify": "",
        "error": "",
        "continue": True,
        "usage": {},
    }
    try:
        outcome = _run_agent_turn(meta, session, prompt, runner)
        response = _parse_agent_response(outcome["message"])
        ended = utc_now()
        delivered_messages = [
            {
                "id": message.get("id") or "",
                "created_at": message.get("created_at") or "",
                "author": message.get("author") or "user",
                "text": message.get("text") or "",
            }
            for message in (session.get("pending_messages") or [])
            if message.get("text")
        ]
        session["thread_id"] = outcome.get("thread_id") or session.get("thread_id") or ""
        session["rollout_path"] = outcome.get("rollout_path") or session.get("rollout_path") or ""
        session["pending_messages"] = []
        usage = _normalize_usage(outcome.get("usage"))
        _add_usage(meta, state, usage, ended)
        state["reply"] = response["reply"]
        state["update"] = response["update"]
        state["last_success_at"] = format_utc(ended)
        state["last_error"] = ""
        state["thread_id"] = session["thread_id"]
        state["wake_requested_at"] = ""
        state["activity"] = response["status"]
        if terminal_status:
            state["status"] = terminal_status
            state["next_wake_at"] = ""
        elif response["continue"]:
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
        run["update"] = response["update"]
        run["notify"] = response["notify"]
        run["continue"] = bool(response["continue"])
        run["usage"] = usage
        run["messages"] = delivered_messages
        _write_run(agent_dir, meta["hostname"], run)
        if response["notify"]:
            title = f"Agent: {meta['name']}"
            Pushover().send(title, response["notify"])
    except Exception as exc:
        ended = utc_now()
        state["status"] = terminal_status or "error"
        state["last_error"] = _single_line(str(exc)) or exc.__class__.__name__
        state["activity"] = state["last_error"]
        state["wake_requested_at"] = ""
        if terminal_status:
            state["next_wake_at"] = ""
        else:
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
        outcome = runner(meta, session, prompt)
        if not isinstance(outcome, dict):
            raise TypeError("runner must return a dict")
        return outcome
    started = utc_now()
    worker = Agent(
        session.get("cwd") or meta.get("cwd"),
        session.get("yolo", True),
        session.get("thread_id") or None,
        session.get("flags") or None,
        include_thinking=False,
        backend=session.get("backend") or None,
        env=_agent_env(meta, session),
    )
    message = worker(prompt)
    usage = worker.last_usage or {}
    rollout_path = ""
    if (session.get("backend") or "codex") == "codex":
        rollout_usage, rollout_path = _codex_rollout_usage(
            session,
            worker.thread_id or session.get("thread_id") or "",
            started,
        )
        if rollout_usage:
            usage = rollout_usage
    return {
        "message": message,
        "thread_id": worker.thread_id or "",
        "usage": usage,
        "rollout_path": rollout_path,
    }


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
    update = payload.get("update")
    notify = payload.get("notify")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Agent response missing string 'status'.")
    if not isinstance(cont, bool):
        raise ValueError("Agent response missing boolean 'continue'.")
    if reply is None:
        reply = ""
    if update is None:
        update = reply or status or ""
    if notify is None:
        notify = ""
    if not isinstance(reply, str):
        raise ValueError("Agent response missing string 'reply'.")
    if not isinstance(update, str):
        raise ValueError("Agent response missing string 'update'.")
    if not isinstance(notify, str):
        raise ValueError("Agent response missing string 'notify'.")
    return {
        "status": _single_line(status),
        "continue": cont,
        "reply": reply.strip(),
        "update": update.strip(),
        "notify": notify.strip(),
    }


def _build_wake_prompt(meta, state, session, now, commands, agent_dir):
    messages = session.get("pending_messages") or []
    book_path = agent_dir / "AGENTBOOK.md"
    wake_mode = _wake_mode(state, session)
    lines = [
        _agent_prompt(wake_mode),
        "",
        f"Wake mode: {wake_mode.replace('_', ' ')}",
        f"Current UTC time: {format_utc(now)}",
        f"Agent name: {meta['name']}",
        f"Stop policy: {meta['stop_policy']}",
        f"Heartbeat minutes: {meta['heartbeat_minutes']}",
        "",
        f"Working directory: {meta['cwd']}",
        f"Agentbook path: {book_path}",
        "Update the agentbook before you respond. Add or revise a dated note when "
        "something durable changed, when you corrected your picture, or when an "
        "assumption needs to be made explicit.",
        "If little has changed across wakes, treat that as evidence about the "
        "situation and reconsider your frame or next action instead of padding the "
        "book.",
        "If a queued message materially changes the durable situation, reflect that in the standing guidance or working notes before moving on.",
    ]
    if _include_full_goal_prompt(state, session):
        lines.extend(["", "Original instructions:", meta["prompt"]])
    book = _ensure_agentbook_header(book_path, meta["prompt"], now)
    if book.strip():
        lines.extend(["", "Agentbook (header + latest notes):", _book_excerpt(book, _AGENTBOOK_BOOK_LIMIT, _AGENTBOOK_HEADER_LIMIT, _AGENTBOOK_TAIL_LIMIT)])
    raw_facts = _wake_facts(state)
    if raw_facts:
        lines.extend(["", "Raw harness facts:"])
        lines.extend(f"- {item}" for item in raw_facts)
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
            if state.get("status") in ("paused", "done"):
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
    if status in ("done", "canceled") and int(state.get("unread_message_count") or 0) > 0:
        return True
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


def _one_shot_terminal_status(state):
    """Return the terminal status when a one-off message wake should run."""
    status = state.get("status") or ""
    if status not in _TERMINAL_STATES:
        return ""
    if int(state.get("unread_message_count") or 0) < 1:
        return ""
    return status


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


def _snapshot(agent_dir, child_map=None):
    meta = _read_json(agent_dir / "meta.json")
    state = _read_json(agent_dir / "state.json")
    session = _read_session(agent_dir)
    runtime = _agent_runtime(agent_dir, meta, state, session)
    queued = _queued_commands(agent_dir)
    queued_controls = [item for item in queued if item.get("kind") != "send"]
    queued_kinds = [item.get("kind") or "" for item in queued_controls if item.get("kind")]
    if child_map is None:
        child_ids = _child_map(agent_dir.parents[1]).get(meta["id"], [])
    else:
        child_ids = child_map.get(meta["id"], [])
    unread = int(state.get("unread_message_count") or 0) + len(
        [item for item in queued if item.get("kind") == "send"]
    )
    status = state.get("status") or ""
    return {
        "id": meta["id"],
        "name": meta["name"],
        "created_at": meta["created_at"],
        "created_by": meta["created_by"],
        "parent_id": meta.get("parent_id") or "",
        "hostname": meta["hostname"],
        "cwd": meta["cwd"],
        "stop_policy": meta["stop_policy"],
        "heartbeat_minutes": meta["heartbeat_minutes"],
        "status": status,
        "display_status": _display_status(
            status,
            queued_kinds,
            runtime["run_lock_held"],
            runtime["stale"],
        ),
        "thread_id": state.get("thread_id") or "",
        "last_wake_at": state.get("last_wake_at") or "",
        "last_success_at": state.get("last_success_at") or "",
        "next_wake_at": state.get("next_wake_at") or "",
        "wake_requested_at": state.get("wake_requested_at") or "",
        "unread_message_count": unread,
        "pending_command_count": len(queued_controls),
        "pending_commands": queued_kinds,
        "input_tokens": int(state.get("input_tokens") or 0),
        "output_tokens": int(state.get("output_tokens") or 0),
        "total_tokens": int(state.get("total_tokens") or 0),
        "avg_tokens_per_hour": float(state.get("avg_tokens_per_hour") or 0.0),
        "child_ids": list(child_ids),
        "last_error": state.get("last_error") or "",
        "activity": state.get("activity") or "",
        "reply": state.get("reply") or "",
        "update": state.get("update") or "",
        "run_lock_held": runtime["run_lock_held"],
        "last_event_at": runtime["last_event_at"],
        "stale": runtime["stale"],
        "stale_after_seconds": runtime["stale_after_seconds"],
        "stale_for_seconds": runtime["stale_for_seconds"],
    }


def _agent_runtime(agent_dir, meta, state, session=None, now=None):
    """Return live wake health for one agent."""
    run_lock_path = agent_dir / "hosts" / meta["hostname"] / "run.lock"
    run_lock_held = _run_lock_held(run_lock_path)
    stale_after_seconds = _stale_after_seconds(meta.get("heartbeat_minutes") or 0)
    info = {
        "run_lock_held": run_lock_held,
        "last_event_at": "",
        "stale": False,
        "stale_after_seconds": stale_after_seconds,
        "stale_for_seconds": 0,
    }
    if (state.get("status") or "") != "running" and not run_lock_held:
        return info
    now = now or utc_now()
    session = session or _read_session(agent_dir)
    thread_id = session.get("thread_id") or state.get("thread_id") or ""
    rollout_path = _resolve_rollout_path(session.get("rollout_path"), thread_id)
    if rollout_path is not None and rollout_path.exists():
        turn = _last_rollout_turn(_rollout_events(rollout_path))
        if turn is not None:
            info["last_event_at"] = turn.get("last_event_at") or turn.get("started_at") or ""
    last_progress_at = parse_utc(info["last_event_at"]) or parse_utc(state.get("last_wake_at"))
    if last_progress_at is None:
        return info
    idle = max(0, int((now - last_progress_at).total_seconds()))
    info["stale_for_seconds"] = idle
    info["stale"] = run_lock_held and idle >= stale_after_seconds
    return info


def _stale_after_seconds(heartbeat_minutes):
    minutes = int(heartbeat_minutes or 0)
    return max(_STALE_MIN_SECONDS, minutes * 60 * _STALE_HEARTBEAT_MULTIPLIER)


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
    for key, value in os.environ.items():
        if key in ("CODEXAPI_AGENT_ID", "CODEXAPI_AGENT_NAME", "CODEXAPI_AGENT_PARENT_ID"):
            continue
        env[key] = value
    if not (env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")):
        gh_token = _gh_auth_token()
        if gh_token:
            env["GH_TOKEN"] = gh_token
    return env


def _gh_auth_token():
    """Return the active gh auth token when available."""
    if shutil.which("gh") is None:
        return ""
    result = subprocess.run(
        ["gh", "auth", "token"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _parent_identity(home, parent_ref):
    """Return the resolved parent agent id and name, if any."""
    if parent_ref is not None and str(parent_ref).strip():
        meta = _read_json(resolve_agent_dir(str(parent_ref), home) / "meta.json")
        return meta["id"], meta["name"]
    parent_id = os.environ.get("CODEXAPI_AGENT_ID", "").strip()
    parent_name = os.environ.get("CODEXAPI_AGENT_NAME", "").strip()
    if parent_id:
        return parent_id, parent_name
    return "", ""


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


def _run_lock_held(path):
    """Return true when the per-agent run lock is currently held."""
    with _try_lock(path) as handle:
        return handle is None


def _lock_info(path):
    try:
        return _read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _signal_run_process(pid, sig):
    """Signal the wake process group when possible, else just the pid."""
    pgid = None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid is not None:
        os.killpg(pgid, sig)
    else:
        os.kill(pid, sig)
    return pgid


def _wait_for_lock_release(path, timeout_seconds):
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if not _run_lock_held(path):
            return True
        time.sleep(_RECOVER_POLL_INTERVAL)
    return not _run_lock_held(path)


def _recover_run_lock(path):
    """Terminate the current lock holder and wait for the wake lock to clear."""
    if not _run_lock_held(path):
        return {
            "pid": None,
            "pgid": None,
            "sent_sigterm": False,
            "sent_sigkill": False,
        }
    info = _lock_info(path)
    pid = _usage_int(info.get("pid"))
    if pid is None:
        raise ValueError("Run lock is held but has no recorded pid.")
    result = {
        "pid": pid,
        "pgid": None,
        "sent_sigterm": False,
        "sent_sigkill": False,
    }
    try:
        result["pgid"] = _signal_run_process(pid, signal.SIGTERM)
        result["sent_sigterm"] = True
    except ProcessLookupError:
        result["pgid"] = None
    if _wait_for_lock_release(path, _RECOVER_TERM_TIMEOUT):
        return result
    result["pgid"] = _signal_run_process(pid, signal.SIGKILL)
    result["sent_sigkill"] = True
    if _wait_for_lock_release(path, _RECOVER_KILL_TIMEOUT):
        return result
    raise ValueError("Run lock stayed held after SIGTERM/SIGKILL.")


def _read_session(agent_dir):
    meta = _read_json(agent_dir / "meta.json")
    return _read_json(agent_dir / "hosts" / meta["hostname"] / "session.json")


def _sync_state_from_session(state, session):
    pending = session.get("pending_messages") or []
    state["unread_message_count"] = len(pending)
    state["thread_id"] = session.get("thread_id") or ""


def _agent_env(meta, session):
    """Return the backend env with stable agent identity added."""
    env = dict(session.get("env") or {})
    env["CODEXAPI_AGENT_ID"] = meta["id"]
    env["CODEXAPI_AGENT_NAME"] = meta["name"]
    if meta.get("parent_id"):
        env["CODEXAPI_AGENT_PARENT_ID"] = meta["parent_id"]
    return env


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


def _include_full_goal_prompt(state, session):
    if not (state.get("last_success_at") or "").strip():
        return True
    return not ((session.get("thread_id") or state.get("thread_id") or "").strip())


def _wake_mode(state, session):
    markers = (
        state.get("last_wake_at"),
        state.get("last_success_at"),
        state.get("reply"),
        state.get("update"),
        state.get("last_error"),
        state.get("thread_id"),
        session.get("thread_id"),
    )
    for value in markers:
        if (value or "").strip():
            return "continuation"
    return "first_wake"


def _agent_prompt(wake_mode):
    if wake_mode == "continuation":
        return _CONTINUATION_PROMPT + _AGENT_PROMPT_TAIL
    return _FIRST_WAKE_PROMPT + _AGENT_PROMPT_TAIL


def _wake_facts(state):
    facts = []
    previous_status = (state.get("activity") or "").strip()
    if previous_status:
        facts.append(f"Previous status: {previous_status}")
    previous_update = (state.get("update") or "").strip()
    if previous_update:
        facts.append(f"Previous update: {previous_update}")
    previous_error = (state.get("last_error") or "").strip()
    if previous_error:
        facts.append(f"Previous error: {previous_error}")
    return facts


def _ensure_agentbook_header(path, prompt, now):
    text = _read_text(path)
    if _agentbook_has_header(text):
        return text
    restored = _restore_agentbook_header(text, prompt, now)
    _write_text(path, restored)
    return restored


def _agentbook_has_header(text):
    text = str(text or "")
    required = (
        "## Purpose",
        "## Values",
        "## Original Goal",
        "## Standing Guidance",
        "## Working Notes",
    )
    return all(section in text for section in required)


def _restore_agentbook_header(text, prompt, now):
    restored = _agentbook_header(prompt).rstrip()
    existing = str(text or "").strip()
    if not existing:
        return restored + "\n"
    stamp = _agentbook_stamp(now)
    return "\n".join(
        [
            restored,
            "",
            f"### {stamp}",
            "System note:",
            "- The durable agentbook header was restored automatically on wake because one or more required sections were missing.",
            "",
            "Recovered notes:",
            existing,
            "",
        ]
    )


def _agentbook_stamp(now):
    if now is None:
        return ""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _book_excerpt(text, limit, header_limit, tail_limit):
    text = str(text or "").strip()
    if not text:
        return ""
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


def _snippet(text, limit):
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _tail_snippet(text, limit):
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return "..." + text[-(limit - 3) :].lstrip()


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


def _normalize_usage(usage):
    """Normalize usage dicts for state accounting."""
    if not isinstance(usage, dict):
        return {}
    input_tokens = _usage_int(usage.get("input_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    normalized = {}
    if input_tokens is not None:
        normalized["input_tokens"] = input_tokens
    if output_tokens is not None:
        normalized["output_tokens"] = output_tokens
    if total_tokens is not None:
        normalized["total_tokens"] = total_tokens
    return normalized


def _add_usage(meta, state, usage, now):
    """Accumulate token usage totals and refresh the running average."""
    if not usage:
        return
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if input_tokens is not None:
        state["input_tokens"] = int(state.get("input_tokens") or 0) + input_tokens
    if output_tokens is not None:
        state["output_tokens"] = int(state.get("output_tokens") or 0) + output_tokens
    if total_tokens is None:
        total_tokens = 0
        if input_tokens is not None:
            total_tokens += input_tokens
        if output_tokens is not None:
            total_tokens += output_tokens
    state["total_tokens"] = int(state.get("total_tokens") or 0) + total_tokens
    created_at = parse_utc(meta.get("created_at"))
    if created_at is None:
        return
    elapsed = (now - created_at).total_seconds()
    if elapsed <= 0:
        elapsed = 1
    state["avg_tokens_per_hour"] = round(state["total_tokens"] * 3600.0 / elapsed, 2)


def _usage_int(value):
    """Return an integer-like usage value or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _display_status(status, pending_commands, run_lock_held, stale):
    """Return a user-facing status that includes queued control intent."""
    state = str(status or "")
    commands = [str(kind or "") for kind in pending_commands or [] if kind]
    if stale:
        return "stale"
    if commands:
        last = commands[-1]
        if last == "resume" and state in ("paused", "done"):
            return "resuming"
        if last == "pause" and state in ("ready", "error", "running"):
            return "pausing"
        if last == "cancel" and state != "canceled":
            return "canceling"
        if last == "wake" and state in ("ready", "error"):
            return "waking"
    if run_lock_held and state == "running":
        return "running"
    return state or ""


def _queued_commands(agent_dir, kind=None):
    """Return queued command payloads from commands/new."""
    queued = []
    new_dir = agent_dir / "commands" / "new"
    if not new_dir.exists():
        return queued
    for path in sorted(new_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = _read_json(path)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        payload_kind = payload.get("kind") or ""
        if kind and payload_kind != kind:
            continue
        queued.append(payload)
    return queued


def _queued_send_commands(agent_dir):
    return _queued_commands(agent_dir, "send")


def _has_new_commands(agent_dir):
    new_dir = agent_dir / "commands" / "new"
    if not new_dir.exists():
        return False
    for path in new_dir.iterdir():
        if path.is_file() and path.suffix == ".json":
            return True
    return False


def _child_map(home):
    """Return parent_id -> [child ids] for this home."""
    root = _resolve_home(home) / "agents"
    child_map = {}
    if not root.exists():
        return child_map
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        try:
            meta = _read_json(agent_dir / "meta.json")
        except FileNotFoundError:
            continue
        parent_id = meta.get("parent_id") or ""
        if not parent_id:
            continue
        child_map.setdefault(parent_id, []).append(meta["id"])
    for child_ids in child_map.values():
        child_ids.sort()
    return child_map


def _agent_brief(home, agent_id, child_map):
    """Return a short snapshot for one related agent."""
    if not agent_id:
        return None
    try:
        agent_dir = resolve_agent_dir(agent_id, home)
    except ValueError:
        return None
    snapshot = _snapshot(agent_dir, child_map)
    return {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "status": snapshot["status"],
        "reply": snapshot["reply"],
    }


def _agent_briefs(home, agent_ids, child_map):
    """Return short snapshots for a list of related agents."""
    return [
        brief
        for brief in (_agent_brief(home, agent_id, child_map) for agent_id in agent_ids or [])
        if brief is not None
    ]


def _codex_rollout_usage(session, thread_id, started_at):
    """Return usage from the current Codex rollout plus its resolved path."""
    if not thread_id:
        return {}, ""
    rollout_path = _resolve_rollout_path(session.get("rollout_path"), thread_id)
    if rollout_path is None:
        return {}, ""
    usage = _extract_rollout_usage(rollout_path, started_at)
    if not usage:
        return {}, str(rollout_path)
    return usage, str(rollout_path)


def _resolve_rollout_path(known_path, thread_id):
    """Return the rollout file for a thread, preferring the cached session path."""
    if known_path:
        path = Path(known_path)
        if path.exists() and (not thread_id or thread_id in path.name):
            return path
    if not thread_id:
        return None
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    if not root.exists():
        return None
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            if thread_id not in name:
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _extract_rollout_usage(path, started_at):
    """Return the latest per-turn token usage written after this wake started."""
    latest = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "event_msg":
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                timestamp = parse_utc(event.get("timestamp"))
                if timestamp is not None and timestamp < started_at:
                    continue
                info = payload.get("info") or {}
                usage = _normalize_usage(info.get("last_token_usage"))
                if usage:
                    latest = usage
    except OSError:
        return {}
    return latest


def _rollout_events(path):
    """Return parsed JSONL events from one rollout file."""
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def _last_rollout_turn(events, include_actions=False):
    """Return the latest task_started slice from rollout events."""
    turn_events = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "task_started":
            turn_events = [event]
            continue
        if turn_events:
            turn_events.append(event)
    if not turn_events:
        return None

    started = turn_events[0]
    started_payload = started.get("payload") or {}
    last_event_at = ""
    progress_events = []
    assistant_events = []
    tools = []
    tool_by_call_id = {}
    ended_at = ""
    task_complete_message = ""

    for event in turn_events:
        last_event_at = event.get("timestamp") or last_event_at
        payload = event.get("payload") or {}
        event_type = event.get("type")
        payload_type = payload.get("type")
        if event_type == "event_msg":
            if payload_type == "agent_message":
                item = {
                    "text": str(payload.get("message") or "").strip(),
                    "phase": payload.get("phase") or "",
                }
                if item["text"]:
                    progress_events.append(item)
            elif payload_type == "task_complete":
                ended_at = event.get("timestamp") or ""
                task_complete_message = str(payload.get("last_agent_message") or "").strip()
        elif event_type == "response_item":
            if payload_type == "message" and payload.get("role") == "assistant":
                text = _response_message_text(payload)
                if text:
                    assistant_events.append(
                        {
                            "text": text,
                            "phase": payload.get("phase") or "",
                        }
                    )
            elif include_actions and payload_type in ("function_call", "custom_tool_call"):
                tool = _rollout_tool_call(payload)
                if tool is None:
                    continue
                tools.append(tool)
                call_id = tool.get("call_id") or ""
                if call_id:
                    tool_by_call_id[call_id] = tool
            elif include_actions and payload_type in ("function_call_output", "custom_tool_call_output"):
                tool = tool_by_call_id.get(payload.get("call_id") or "")
                if tool is not None:
                    _apply_rollout_tool_output(tool, payload)

    visible = progress_events or assistant_events
    progress = [item["text"] for item in visible]
    final_output = visible[-1]["text"] if visible else task_complete_message
    final_json = None
    if final_output:
        final_json = _parse_rollout_final_json(final_output)
        if progress and progress[-1] == final_output and (
            final_json is not None or (visible[-1].get("phase") or "") == "final_answer"
        ):
            progress = progress[:-1]

    if include_actions:
        for tool in tools:
            tool["summary"] = _rollout_tool_summary(tool)

    return {
        "turn_id": started_payload.get("turn_id") or "",
        "started_at": started.get("timestamp") or "",
        "ended_at": ended_at,
        "last_event_at": last_event_at,
        "progress": progress,
        "tools": tools,
        "final_output": final_output,
        "final_json": final_json,
    }


def _response_message_text(payload):
    """Return the text content from one assistant response message."""
    parts = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _rollout_tool_call(payload):
    """Return a compact tool-call record for one rollout item."""
    name = payload.get("name") or ""
    kind = payload.get("type") or ""
    call_id = payload.get("call_id") or ""
    tool = {
        "call_id": call_id,
        "kind": kind,
        "name": name,
        "command": "",
        "files": [],
        "exit_code": None,
        "output": "",
        "summary": "",
    }
    if kind == "function_call":
        arguments = _parse_rollout_json(payload.get("arguments"))
        if isinstance(arguments, dict):
            tool["command"] = str(arguments.get("cmd") or "").strip()
    elif kind == "custom_tool_call" and name == "apply_patch":
        tool["files"] = _patch_targets(payload.get("input") or "")
    return tool


def _apply_rollout_tool_output(tool, payload):
    """Fold tool output into a compact rollout tool record."""
    text, exit_code = _tool_output_details(payload.get("output"))
    if exit_code is not None:
        tool["exit_code"] = exit_code
    tool["output"] = _snippet(text.strip(), 400) if text else ""
    if tool["name"] == "apply_patch" and not tool["files"]:
        tool["files"] = _updated_files(text)


def _parse_rollout_json(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _parse_rollout_final_json(text):
    """Return the normalized final agent JSON when the text matches the contract."""
    try:
        return _parse_agent_response(text)
    except ValueError:
        return None


def _tool_output_details(output):
    """Return normalized output text and exit code from a rollout tool result."""
    text = str(output or "")
    payload = _parse_rollout_json(text)
    exit_code = None
    if isinstance(payload, dict):
        metadata = payload.get("metadata") or {}
        exit_code = _usage_int(metadata.get("exit_code"))
        text = str(payload.get("output") or "")
    raw = text
    body = raw
    if "\nOutput:\n" in raw:
        body = raw.split("\nOutput:\n", 1)[1]
    elif raw.startswith("Output:\n"):
        body = raw.split("Output:\n", 1)[1]
    for line in raw.splitlines():
        if not line.startswith("Process exited with code "):
            continue
        tail = line.rsplit(" ", 1)[-1].strip()
        if tail.startswith("-"):
            tail = tail[1:]
        if tail.isdigit():
            exit_code = int(line.rsplit(" ", 1)[-1].strip())
            break
    return body.strip(), exit_code


def _rollout_tool_summary(tool):
    """Return one readable summary line for a tool action."""
    name = tool.get("name") or ""
    exit_code = tool.get("exit_code")
    suffix = ""
    if exit_code is not None:
        suffix = f" (exit {exit_code})"
    if name == "exec_command":
        command = _single_line(_snippet(tool.get("command") or "", 140))
        if command:
            return f"Running command: {command}{suffix}"
        return f"Running command{suffix}"
    if name == "apply_patch":
        files = tool.get("files") or []
        if files:
            label = ", ".join(files[:3])
            if len(files) > 3:
                label += ", ..."
            return f"Editing files: {label}{suffix}"
        return f"Editing files{suffix}"
    if name:
        return f"{name}{suffix}"
    return f"tool{suffix}"


def _patch_targets(text):
    """Return patch target files from an apply_patch input."""
    files = []
    for line in str(text or "").splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if not line.startswith(prefix):
                continue
            target = line[len(prefix) :].strip()
            if target and target not in files:
                files.append(target)
    return files


def _updated_files(text):
    """Return file paths mentioned in apply_patch output."""
    files = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line == "Success. Updated the following files:":
            continue
        if line.startswith(("M ", "A ", "D ")):
            target = line[2:].strip()
            if target and target not in files:
                files.append(target)
    return files


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


def _remove_cron_line(existing, tag):
    lines = []
    changed = False
    for raw in str(existing or "").splitlines():
        if raw.strip().endswith(f"# {tag}"):
            changed = True
            continue
        lines.append(raw)
    text = "\n".join(lines)
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


def _remove_file(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
