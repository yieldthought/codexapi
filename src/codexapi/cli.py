import argparse
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import termios
import tty
from datetime import datetime
from pathlib import Path

from . import __version__
from .agent import Agent, agent
from .agents import (
    codexapi_home,
    control_agent,
    cron_status as agent_cron_status,
    current_hostname,
    delete_agent as delete_managed_agent,
    install_cron as install_agent_cron,
    list_agents as list_managed_agents,
    nudge_agent,
    read_agent as read_managed_agent,
    read_agentbook,
    recover_agent as recover_managed_agent,
    run_agent as run_managed_agent,
    send_agent,
    set_agent_heartbeat,
    show_agent as show_managed_agent,
    status_agent as status_managed_agent,
    start_agent as start_managed_agent,
    tick as tick_managed_agents,
    uninstall_cron as uninstall_agent_cron,
)
from .foreach import foreach
from .ralph import Ralph, cancel_ralph_loop
from .science import Science
from .task import DEFAULT_MAX_ITERATIONS, TaskFailed, task
from .taskfile import TaskFile, load_task_file, task_def_uses_item
from .rate_limits import quota_line
from .lead import lead

_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)([smhdSMHD]?)\s*$")
_TAIL_BYTES = 256 * 1024
_TAIL_MAX_BYTES = 4 * 1024 * 1024
_TAIL_MIN_LINES = 200
_PROJECT_LOOP_SLEEP = 30
_ROLL_OUT_PREFIX = "rollout-"
_TASK_TEMPLATE = (
    "prompt: |\n"
    "  Main task prompt. Required. Use {{item}} for per-item values.\n"
    "  Describe what the agent should do here.\n"
    "\n"
    "set_up: |\n"
    "  Optional setup steps before the task runs.\n"
    "  Example: create a branch for {{item}} and switch to it.\n"
    "\n"
    "check: |\n"
    "  Optional verification prompt. Use \"None\" to skip verification.\n"
    "  If this section is not present, an automatic one based on the prompt will be used.\n"
    "  Example: run pytest and check all tests pass with no skips or cheats, check README.md updated.\n"
    "\n"
    "on_success: |\n"
    "  Optional follow-up instructions after a successful task.\n"
    "  Example: add and commit changes and use 'gh' to open a PR.\n"
    "\n"
    "on_failure: |\n"
    "  Optional follow-up instructions after a failed task.\n"
    f"  Example: revert changes and abandon the new branch.\n"
    "\n"
    "tear_down: |\n"
    "  Optional cleanup steps after the task finishes.\n"
    "  Example: remove any temporary or untracked files and change back to the main branch.\n"
    "\n"
    "max_iterations: 10  # Optional (default is 10). 0 means unlimited.\n"
)
_TOOL_LABELS = {
    "apply_patch": "Editing files",
    "exec_command": "Running command",
    "list_mcp_resources": "Listing resources",
    "list_mcp_resource_templates": "Listing templates",
    "read_mcp_resource": "Reading resource",
    "view_image": "Viewing image",
    "write_stdin": "Writing to session",
}
_COLUMN_TITLES = {
    "id": "ID",
    "status": "STAT",
    "tok": "TOK/S",
    "in": "IN",
    "out": "OUT",
    "turn": "TURN",
    "turns": "NTRN",
    "model": "MODEL",
    "effort": "EFF",
    "perm": "PERM",
    "cwd": "CWD",
}
_FOREACH_STATUS_MARKERS = {"⏳", "✅", "❌"}


def _add_subparser(subparsers, name, help_text, **kwargs):
    parser_kwargs = dict(kwargs)
    parser_kwargs["help"] = help_text
    if help_text is not argparse.SUPPRESS:
        parser_kwargs.setdefault("description", help_text)
    return subparsers.add_parser(name, **parser_kwargs)


def _read_prompt(prompt):
    if prompt and prompt != "-":
        return prompt

    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("No prompt provided. Pass a prompt or pipe via stdin.")
    return data


def _parse_duration_seconds(value, flag_name):
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        raise SystemExit(f"{flag_name} cannot be empty.")
    match = _DURATION_RE.match(text)
    if not match:
        raise SystemExit(
            f"{flag_name} must be a number with optional unit s/m/h/d (example: 90m)."
        )
    amount = float(match.group(1))
    unit = (match.group(2) or "m").lower()
    if amount < 0:
        raise SystemExit(f"{flag_name} must be >= 0.")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return amount * multiplier


def _read_prompt_file(path):
    if not path or not str(path).strip():
        raise SystemExit("Prompt file path is empty.")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise SystemExit(f"Prompt file not found: {path}") from None
    if not data.strip():
        raise SystemExit(f"Prompt file is empty: {path}")
    return data


def _single_line(text):
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").split())


def _print_managed_agent_list(items):
    if not items:
        print("No agents.")
        return
    print("ID       STAT      POL  HOST         QMSG QCMD TOKENS  TOK/H   NEXT REPO         NAME")
    for item in items:
        ident = item["id"][:8]
        status = _truncate_head(item.get("display_status") or item.get("status") or "-", 9)
        policy = _truncate_head(_policy_label(item.get("stop_policy")), 4)
        host = _truncate_head(item["hostname"] or "-", 12)
        queued_messages = str(item["unread_message_count"])
        queued_commands = str(int(item.get("pending_command_count") or 0))
        tokens = _format_token_total(item["total_tokens"])
        tok_h = _format_token_rate(item.get("avg_tokens_per_hour"))
        next_wake = _truncate_head(_next_wake_label(item), 6)
        repo = _truncate_head(_repo_label(item.get("cwd")), 12)
        name = item["name"]
        print(
            f"{ident:<8} {status:<9} {policy:<4} {host:<12} {queued_messages:>4} {queued_commands:>4} {tokens:>6} {tok_h:>7} {next_wake:>6} {repo:<12} {name}"
        )


def _print_managed_agent_read(result):
    print(f"{result['name']} [{result['status']}]")
    items = result.get("items") or []
    if not items:
        print("No messages.")
        return
    for item in items:
        stamp = item.get("timestamp") or "-"
        kind = item.get("kind") or "item"
        author = item.get("author") or ""
        if author:
            print(f"[{stamp}] {kind} {author}:")
        else:
            print(f"[{stamp}] {kind}:")
        print(item.get("text") or "")
        print()


def _print_managed_agent_status(result, include_actions=False):
    print(f"{result['name']} [{result['agent_status'] or '-'}]")
    print(f"ID: {result['id']}")
    print(f"State: {result.get('state_status') or '-'}")
    print(f"Thread: {result.get('thread_id') or '-'}")
    print(f"Turn: {result.get('turn_id') or '-'} [{result.get('turn_state') or '-'}]")
    print(f"Started: {result.get('started_at') or '-'}")
    print(f"Ended: {result.get('ended_at') or '-'}")
    print(f"CWD: {result.get('cwd') or '-'}")
    print(f"Rollout: {result.get('rollout_path') or '-'}")
    print(f"Last event: {result.get('last_event_at') or '-'}")
    print(f"Stale: {_stale_text(result)}")
    print(f"Queued messages: {result.get('queued_message_count') or 0}")
    print(f"Pending commands: {_pending_commands_text(result.get('pending_commands'))}")
    progress = result.get("progress") or []
    print("Progress:")
    if not progress:
        print("- none")
    else:
        for item in progress:
            print(f"- {_single_line(item)}")
    if include_actions:
        tools = result.get("tools") or []
        print("Actions:")
        if not tools:
            print("- none")
        else:
            for tool in tools:
                print(f"- {tool.get('summary') or tool.get('name') or 'tool'}")
                if tool.get("output"):
                    print(f"  Output: {_single_line(tool['output'])}")
    final_json = result.get("final_json")
    if final_json is not None:
        print("Final fields:")
        print(f"Status: {final_json.get('status') or '-'}")
        print(f"Continue: {str(bool(final_json.get('continue'))).lower()}")
        print(f"Reply: {final_json.get('reply') or '-'}")
        print(f"Update: {final_json.get('update') or '-'}")
        print(f"Notify: {final_json.get('notify') or '-'}")
        return
    final_output = result.get("final_output") or ""
    print("Final output:")
    if final_output:
        print(final_output)
    else:
        print("-")


def _print_managed_agent_identity():
    override = os.environ.get("CODEXAPI_HOSTNAME", "").strip()
    print(f"Host: {current_hostname()}")
    print(f"Host override: {override or '-'}")
    print(f"Home: {codexapi_home()}")


def _agent_install_cron_command():
    parts = []
    home = os.environ.get("CODEXAPI_HOME", "").strip()
    host = os.environ.get("CODEXAPI_HOSTNAME", "").strip()
    if home:
        parts.append(f"CODEXAPI_HOME={shlex.quote(home)}")
    if host:
        parts.append(f"CODEXAPI_HOSTNAME={shlex.quote(host)}")
    parts.extend(["codexapi", "agent", "install-cron"])
    return " ".join(parts)


def _warn_agent_scheduler_missing():
    try:
        status = agent_cron_status()
    except Exception as exc:
        print(
            "Warning: could not verify whether the codexapi agent scheduler hook is installed.",
            file=sys.stderr,
        )
        print(f"Reason: {exc}", file=sys.stderr)
        print(f"Install it with: {_agent_install_cron_command()}", file=sys.stderr)
        return
    if status["healthy"]:
        return
    if status["configured"]:
        print(
            "Warning: the codexapi agent scheduler hook is installed but not runnable for this CODEXAPI_HOME.",
            file=sys.stderr,
        )
        if status["reason"]:
            print(f"Reason: {status['reason']}", file=sys.stderr)
        print(f"Reinstall it with: {_agent_install_cron_command()}", file=sys.stderr)
        return
    print(
        "Warning: no codexapi agent scheduler hook is installed for this CODEXAPI_HOME. "
        "Background agent wakes will not run until you install it.",
        file=sys.stderr,
    )
    print(f"Install it with: {_agent_install_cron_command()}", file=sys.stderr)


def _system_tick():
    return {"agents": tick_managed_agents()}


def _send_reply_info(agent_ref, message_id):
    """Return the matching run reply for one sent message, if already delivered."""
    shown = show_managed_agent(agent_ref)
    for run in shown.get("recent_runs") or []:
        for message in run.get("messages") or []:
            if message.get("id") != message_id:
                continue
            info = {
                "delivered": True,
                "agent_status": run.get("status") or "",
                "run_id": run.get("id") or "",
            }
            reply = run.get("reply") or ""
            update = run.get("update") or ""
            error = run.get("error") or ""
            if reply:
                info["agent_reply"] = reply
            if update:
                info["agent_update"] = update
            if error:
                info["agent_error"] = error
            return info
    return None


def _print_managed_agent_show(result):
    meta = result["meta"]
    state = result["state"]
    print(f"{meta['name']} [{result.get('display_status') or state.get('status') or '-'}]")
    print(f"ID: {meta['id']}")
    print(f"Host: {meta['hostname']}")
    print(f"Created: {meta['created_at']} by {meta['created_by']}")
    print(f"Parent: {_related_label(result.get('parent'))}")
    print(f"Children: {_children_label(result.get('children'))}")
    print(
        f"Policy: {meta['stop_policy']}  Heartbeat: {meta['heartbeat_minutes']}m  Qmsg: {result['unread_message_count']}  Qcmd: {result.get('pending_command_count') or 0}"
    )
    print(f"CWD: {meta['cwd']}")
    print(f"Agentbook: {result.get('agentbook_path') or '-'}")
    print(f"State: {state.get('status') or '-'}")
    print(f"Thread: {state.get('thread_id') or '-'}")
    print(f"Last event: {result.get('last_event_at') or '-'}")
    print(f"Stale: {_stale_text(result)}")
    print(f"Pending commands: {_pending_commands_text(result.get('pending_commands'))}")
    print(
        "Tokens: "
        f"{_format_token_total(state.get('total_tokens'))} total "
        f"({_format_token_total(state.get('input_tokens'))} in, "
        f"{_format_token_total(state.get('output_tokens'))} out, "
        f"{_format_token_rate(state.get('avg_tokens_per_hour'))}/h)"
    )
    print(f"Activity: {_state_text(state.get('activity'))}")
    print(f"Reply: {_state_text(state.get('reply'))}")
    print(f"Update: {_state_text(state.get('update'))}")
    print(f"Last error: {_state_text(state.get('last_error'))}")
    print(f"Last wake: {_state_time(state.get('last_wake_at'))}")
    print(f"Last success: {_state_time(state.get('last_success_at'))}")
    print(f"Next wake: {_state_time(state.get('next_wake_at'))}")
    print(f"Wake requested: {_state_time(state.get('wake_requested_at'))}")
    print(f"Prompt: {_truncate_head(_single_line(meta.get('prompt') or ''), 160) or '-'}")
    recent_runs = result.get("recent_runs") or []
    if not recent_runs:
        return
    print()
    print("Recent runs:")
    for run in recent_runs:
        print(_format_managed_agent_run(run))


def _print_managed_agent_book(result):
    print(f"Agentbook: {result['path']}")
    text = result.get("text") or ""
    if text:
        print()
        print(text, end="" if text.endswith("\n") else "\n")
        return
    print()
    print("(empty)")



def _create_task_template(path):
    if not isinstance(path, str) or not path.strip():
        raise SystemExit("create requires a filename.")
    target = Path(path)
    if target.suffix not in (".yaml", ".yml"):
        target = Path(f"{target}.yaml")
    if target.exists():
        if target.is_dir():
            raise SystemExit(f"{target} is a directory.")
        raise SystemExit(f"{target} already exists.")
    try:
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(_TASK_TEMPLATE)
    except FileNotFoundError:
        raise SystemExit(f"Directory does not exist: {target.parent}") from None
    print(target)


def _truncate_head(text, limit):
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _truncate_tail(text, limit):
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return "..." + text[-(limit - 3) :]


def _repo_label(cwd):
    if not isinstance(cwd, str) or not cwd:
        return "-"
    name = Path(cwd).name
    return name or cwd


def _policy_label(stop_policy):
    if stop_policy == "until_done":
        return "done"
    if stop_policy == "until_stopped":
        return "loop"
    return "-"


def _next_wake_label(item):
    status = item.get("status") or ""
    display_status = item.get("display_status") or status
    if status == "running":
        if item.get("stale"):
            return "stale"
        if item.get("run_lock_held"):
            return "run"
        return "lost"
    if display_status in ("resuming", "waking", "pausing", "canceling"):
        return "wake"
    if status in ("done", "canceled"):
        return "-"
    if status == "paused":
        return "paused"
    if item.get("wake_requested_at"):
        return "wake"
    next_wake = _parse_timestamp(item.get("next_wake_at"))
    if next_wake is None:
        return "-"
    now = datetime.now()
    if next_wake <= now:
        return "due"
    return _short_duration((next_wake - now).total_seconds())


def _short_duration(seconds):
    if seconds <= 0:
        return "due"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    if days:
        return f"{days}d"
    hours, rem = divmod(rem, 3600)
    if hours:
        return f"{hours}h"
    minutes, secs = divmod(rem, 60)
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _state_text(value):
    text = _single_line(str(value or ""))
    return text or "-"


def _state_time(value):
    return value or "-"


def _stale_text(item):
    if not item.get("run_lock_held"):
        return "no"
    threshold = _format_duration(item.get("stale_after_seconds"))
    idle = _format_duration(item.get("stale_for_seconds"))
    if item.get("stale"):
        return f"yes ({idle} idle; threshold {threshold})"
    return f"no ({idle} idle; threshold {threshold})"


def _pending_commands_text(value):
    commands = [str(item or "") for item in value or [] if str(item or "").strip()]
    if not commands:
        return "-"
    return ", ".join(commands)


def _format_managed_agent_run(run):
    started = run.get("started_at") or "-"
    reason = run.get("wake_reason") or "-"
    usage = run.get("usage") or {}
    tokens = _format_token_total(usage.get("total_tokens"))
    status = run.get("error") or run.get("status") or "-"
    reply = run.get("reply") or ""
    update = run.get("update") or ""
    message_count = len(run.get("messages") or [])
    parts = [started, reason, tokens]
    if message_count:
        parts.append(f"msgs={message_count}")
    summary = _truncate_head(_single_line(status), 60)
    if update:
        summary = _truncate_head(f"{summary} | {_single_line(update)}", 100)
    if reply:
        summary = _truncate_head(f"{summary} | {_single_line(reply)}", 100)
    parts.append(summary)
    return "- " + "  ".join(parts)


def _related_label(agent):
    if not agent:
        return "-"
    ident = agent.get("id", "")[:8]
    name = agent.get("name") or ident or "-"
    status = agent.get("status") or "-"
    return f"{name} [{status}] {ident}"


def _children_label(children):
    if not children:
        return "-"
    labels = [_related_label(child) for child in children[:3]]
    if len(children) > 3:
        labels.append(f"+{len(children) - 3} more")
    return ", ".join(labels)


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone().replace(tzinfo=None)


def _tail_lines(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            data = b""
            while remaining > 0 and len(data) < _TAIL_MAX_BYTES:
                chunk = min(_TAIL_BYTES, remaining)
                remaining -= chunk
                handle.seek(remaining)
                data = handle.read(chunk) + data
                if data.count(b"\n") >= _TAIL_MIN_LINES:
                    break
    except OSError:
        return []

    if not data:
        return []
    if remaining > 0:
        parts = data.split(b"\n", 1)
        if len(parts) == 2:
            data = parts[1]
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()


def _count_turns(path):
    event_count = 0
    response_count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "\"type\":\"event_msg\"" in line and "\"type\":\"user_message\"" in line:
                    event_count += 1
                    continue
                if "\"type\":\"response_item\"" in line and "\"role\":\"user\"" in line and "\"type\":\"message\"" in line:
                    response_count += 1
    except OSError:
        return None

    if event_count:
        return event_count
    if response_count:
        return response_count
    return None


def _extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return ""


def _activity_title(text):
    if not isinstance(text, str):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    if first.startswith("**") and first.endswith("**") and len(first) > 4:
        return first[2:-2].strip()
    if first.startswith("**"):
        end = first.find("**", 2)
        if end != -1:
            title = first[2:end].strip()
            if title:
                return title
    if first.lstrip().startswith("#"):
        title = first.lstrip("#").strip()
        if title:
            return title
    if first[0] in "-*•":
        title = first[1:].strip()
        if title:
            return title
    return first


def _extract_reasoning(payload):
    if not isinstance(payload, dict):
        return ""
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = []
        for item in summary:
            if not isinstance(item, dict):
                continue
            text = item.get("summary_text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
    content = payload.get("content")
    return _extract_text(content)


def _parse_call_args(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _tool_activity(name, payload):
    label = _TOOL_LABELS.get(name, f"Running {name}")
    status = payload.get("status") if isinstance(payload, dict) else None
    details = ""
    if name == "exec_command":
        args = _parse_call_args(payload.get("arguments"))
        cmd = args.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            details = cmd.strip()
    if details:
        label = f"{label}: {details}"
    if status:
        label = f"{label} ({status})"
    return label


def _session_id(path):
    match = _SESSION_ID_RE.search(path.name)
    if match:
        return match.group(0)
    return path.stem


def _is_session_file(path, root_str):
    if not path.startswith(root_str):
        return False
    name = os.path.basename(path)
    return name.startswith(_ROLL_OUT_PREFIX) and name.endswith(".jsonl")


def _list_codex_processes():
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,uid=,comm=,args="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    current_uid = os.getuid()
    processes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        try:
            ppid = int(parts[1])
        except ValueError:
            continue
        try:
            uid = int(parts[2])
        except ValueError:
            continue
        if uid != current_uid:
            continue
        comm = parts[3]
        args = parts[4] if len(parts) > 4 else ""
        if comm == "codex" or re.search(r"(^|[\\s/])codex(\\s|$)", args):
            processes.append({"pid": pid, "ppid": ppid, "comm": comm, "args": args})
    return processes


def _process_session_files(pid, root):
    root_str = str(root)
    paths = set()
    if shutil.which("lsof"):
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if not line.startswith("n"):
                    continue
                path = line[1:]
                if _is_session_file(path, root_str):
                    paths.add(Path(path))
        return paths

    proc_fd = Path(f"/proc/{pid}/fd")
    if proc_fd.exists():
        try:
            entries = list(proc_fd.iterdir())
        except OSError:
            return paths
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if _is_session_file(target, root_str):
                paths.add(Path(target))
    return paths


def _tokens_per_second(events):
    if len(events) < 2:
        return None
    (start_ts, _start_usage), (end_ts, end_usage) = events[-2], events[-1]
    delta = (end_ts - start_ts).total_seconds()
    if delta <= 0:
        return None
    output_tokens = end_usage.get("output_tokens")
    if not isinstance(output_tokens, int):
        output_tokens = end_usage.get("total_tokens")
    if not isinstance(output_tokens, int):
        return None
    return output_tokens / delta


def _format_token_total(value):
    if value is None:
        return "-"
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "-"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}b"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _format_token_rate(value):
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _format_duration(seconds):
    if seconds is None:
        return "-"
    if seconds < 0:
        return "-"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _summarize_session(path, mtime):
    prompt = None
    prompt_fallback = None
    output = None
    output_fallback = None
    output_ts = None
    output_fallback_ts = None
    token_events = []
    last_user_ts = None
    last_agent_ts = None
    last_event_ts = None
    last_event_kind = None
    last_reasoning = None
    last_reasoning_ts = None
    last_summary = None
    last_summary_ts = None
    last_tool = None
    last_tool_ts = None
    total_usage = None
    meta = {}
    subagent = None
    turns = _count_turns(path)

    for line in _tail_lines(path):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = _parse_timestamp(data.get("timestamp"))
        if timestamp:
            last_event_ts = timestamp
        if data.get("type") == "event_msg":
            payload = data.get("payload") or {}
            kind = payload.get("type")
            last_event_kind = kind
            if kind == "user_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    prompt = message
                if timestamp:
                    last_user_ts = timestamp
            elif kind == "agent_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    output = message
                    if timestamp:
                        output_ts = timestamp
                if timestamp:
                    last_agent_ts = timestamp
            elif kind == "agent_reasoning":
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    last_reasoning = _activity_title(text) or text
                    if timestamp:
                        last_reasoning_ts = timestamp
            elif kind == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    usage = info.get("last_token_usage")
                    if isinstance(usage, dict) and timestamp:
                        token_events.append((timestamp, usage))
                    total = info.get("total_token_usage")
                    if isinstance(total, dict):
                        total_usage = total
        elif data.get("type") == "response_item":
            payload = data.get("payload") or {}
            if payload.get("type") == "message":
                role = payload.get("role")
                text = _extract_text(payload.get("content"))
                if role == "user" and text:
                    prompt_fallback = text
                    if timestamp:
                        last_user_ts = timestamp
                elif role == "assistant" and text:
                    output_fallback = text
                    if timestamp:
                        output_fallback_ts = timestamp
                    if timestamp:
                        last_agent_ts = timestamp
            elif payload.get("type") == "reasoning":
                text = _extract_reasoning(payload)
                if text:
                    last_summary = _activity_title(text) or text
                    if timestamp:
                        last_summary_ts = timestamp
            elif payload.get("type") in ("custom_tool_call", "function_call"):
                name = payload.get("name")
                if isinstance(name, str) and name:
                    last_tool = _tool_activity(name, payload)
                    if timestamp:
                        last_tool_ts = timestamp
        elif data.get("type") == "turn_context":
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                meta.update(payload)
                last_event_kind = "turn_context"
        elif data.get("type") == "session_meta":
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                meta.setdefault("cwd", payload.get("cwd"))
                meta.setdefault("model_provider", payload.get("model_provider"))
                source = payload.get("source")
                if isinstance(source, dict):
                    meta.setdefault("source", source)
                    subagent = source.get("subagent") or subagent
                last_event_kind = "session_meta"

    if not prompt:
        prompt = prompt_fallback or ""
    if not output:
        output = output_fallback or ""
        output_ts = output_fallback_ts

    if subagent:
        meta.setdefault("subagent", subagent)

    activity = ""
    cutoff = last_user_ts
    for text, ts in (
        (last_reasoning, last_reasoning_ts),
        (last_summary, last_summary_ts),
        (last_tool, last_tool_ts),
        (output, output_ts),
    ):
        if not text:
            continue
        if cutoff and (not ts or ts < cutoff):
            continue
        activity = text
        break

    return {
        "id": _session_id(path),
        "prompt": prompt,
        "output": output,
        "activity": activity,
        "tok_s": _tokens_per_second(token_events),
        "total_usage": total_usage,
        "mtime": mtime,
        "last_event_ts": last_event_ts,
        "last_user_ts": last_user_ts,
        "last_agent_ts": last_agent_ts,
        "last_event_kind": last_event_kind,
        "turns": turns,
        "meta": meta,
    }


def _session_status(session):
    last_user = session.get("last_user_ts")
    last_agent = session.get("last_agent_ts")
    if last_user and (not last_agent or last_user > last_agent):
        return "running"
    if last_agent:
        return "idle"
    if session.get("last_event_kind") in ("agent_reasoning", "token_count"):
        return "running"
    return "idle"


def _active_sessions(root):
    if not root.exists():
        return []
    processes = _list_codex_processes()
    if not processes:
        return []
    by_pid = {proc["pid"]: proc for proc in processes}
    children = {pid: [] for pid in by_pid}
    for proc in processes:
        ppid = proc.get("ppid")
        if ppid in children:
            children[ppid].append(proc["pid"])
    for pid in children:
        children[pid].sort()
    sessions = []
    seen = set()
    sessions_by_pid = {}

    def is_subagent(entry):
        meta = entry.get("meta") or {}
        subagent = meta.get("subagent")
        if isinstance(subagent, str) and subagent:
            return True
        source = meta.get("source")
        if isinstance(source, dict):
            subagent = source.get("subagent")
            return isinstance(subagent, str) and subagent
        return False

    for proc in processes:
        entries = []
        for path in _process_session_files(proc["pid"], root):
            if path in seen:
                continue
            seen.add(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            info = _summarize_session(path, mtime)
            info["status"] = _session_status(info)
            entries.append(info)
        entries.sort(key=lambda item: item["mtime"], reverse=True)
        if entries:
            for index, entry in enumerate(entries):
                if not is_subagent(entry):
                    entries.insert(0, entries.pop(index))
                    break
        if entries:
            sessions_by_pid[proc["pid"]] = entries

    cache = {}

    def subtree_mtime(pid):
        if pid in cache:
            return cache[pid]
        latest = 0
        for entry in sessions_by_pid.get(pid, []):
            latest = max(latest, entry["mtime"])
        for child in children.get(pid, []):
            latest = max(latest, subtree_mtime(child))
        cache[pid] = latest
        return latest

    roots = [pid for pid, proc in by_pid.items() if proc.get("ppid") not in by_pid]
    roots.sort(key=subtree_mtime, reverse=True)

    def add_pid(pid, depth):
        entries = sessions_by_pid.get(pid, [])
        has_entry = bool(entries)
        for index, entry in enumerate(entries):
            entry["depth"] = depth + (1 if index else 0)
            sessions.append(entry)
        child_depth = depth + 1 if has_entry else depth
        for child in children.get(pid, []):
            add_pid(child, child_depth)

    for root_pid in roots:
        add_pid(root_pid, 0)

    return sessions


def _permission_label(meta):
    approval = meta.get("approval_policy") if isinstance(meta, dict) else None
    sandbox = meta.get("sandbox_policy") if isinstance(meta, dict) else None
    if isinstance(sandbox, dict):
        sandbox = sandbox.get("type")
    if not approval and not sandbox:
        return "-"
    approval = approval or "-"
    sandbox = sandbox or "-"
    return f"{approval}/{sandbox}"


def _layout_columns(width, id_width, show):
    columns = [
        ("id", "<"),
        ("status", "<"),
        ("tok", ">"),
        ("in", ">"),
        ("out", ">"),
        ("turn", ">"),
        ("turns", ">"),
    ]
    widths = {
        "id": id_width,
        "status": 4,
        "tok": 7,
        "in": 7,
        "out": 7,
        "turn": 7,
        "turns": 5,
    }
    mins = {}

    if show.get("model"):
        columns.append(("model", "<"))
        widths["model"] = 12
        mins["model"] = 8
    if show.get("effort"):
        columns.append(("effort", "<"))
        widths["effort"] = 6
        mins["effort"] = 4
    if show.get("perm"):
        columns.append(("perm", "<"))
        widths["perm"] = 12
        mins["perm"] = 8
    if show.get("cwd", True):
        columns.append(("cwd", "<"))
        widths["cwd"] = 24
        mins["cwd"] = 10

    def available():
        fixed = sum(
            widths[key]
            for key in widths
        )
        return width - (fixed + len(widths) + 3)

    avail = available()
    target = 40
    need = target - avail
    for key in ("cwd", "perm", "model", "effort"):
        if need <= 0:
            break
        if key not in widths:
            continue
        current = widths[key]
        minimum = mins.get(key, current)
        if current > minimum:
            drop = min(current - minimum, need)
            widths[key] -= drop
            need -= drop

    avail = available()
    if avail < 20:
        avail = 20
    prompt_max = max(10, min(40, avail // 3))
    output_max = max(10, avail - prompt_max)
    widths["prompt"] = prompt_max
    widths["output"] = output_max
    return {
        "columns": columns,
        "widths": widths,
    }


def _format_session(session, layout):
    widths = layout["widths"]
    depth = session.get("depth", 0)
    session_id = (" " * depth) + session["id"][:8]
    status = "RUN" if session.get("status") == "running" else "IDLE"
    tok_s = session["tok_s"]
    tok_s_str = "-" if tok_s is None else f"{tok_s:5.1f}"
    last_user_ts = session.get("last_user_ts")
    last_agent_ts = session.get("last_agent_ts")
    if status == "RUN":
        turn_seconds = (datetime.now() - last_user_ts).total_seconds() if last_user_ts else None
    else:
        if last_user_ts and last_agent_ts:
            turn_seconds = (last_agent_ts - last_user_ts).total_seconds()
        else:
            turn_seconds = None
    turn_str = _format_duration(turn_seconds)
    turns = session.get("turns")
    turns_str = "-" if turns is None else str(turns)
    meta = session.get("meta") or {}
    model = meta.get("model") or meta.get("model_provider") or "-"
    effort = meta.get("effort") or "-"
    perm = _permission_label(meta)
    cwd = meta.get("cwd") or "-"
    prompt = _single_line(session["prompt"]) or "-"
    activity = _single_line(session.get("activity") or session["output"]) or "-"
    total_usage = session.get("total_usage") or {}
    total_in = _format_token_total(total_usage.get("input_tokens"))
    total_out = _format_token_total(total_usage.get("output_tokens"))

    values = {
        "id": session_id,
        "status": status,
        "tok": tok_s_str,
        "in": total_in,
        "out": total_out,
        "turn": turn_str,
        "turns": _truncate_head(str(turns_str), widths.get("turns", 0)),
        "model": _truncate_head(str(model), widths.get("model", 0)),
        "effort": _truncate_head(str(effort), widths.get("effort", 0)),
        "perm": _truncate_head(str(perm), widths.get("perm", 0)),
        "cwd": _truncate_tail(str(cwd), widths.get("cwd", 0)),
    }

    parts = []
    for key, align in layout["columns"]:
        width = widths[key]
        value = values.get(key, "")
        parts.append(f"{value:{align}{width}}")

    prompt = _truncate_head(prompt, widths["prompt"])
    prompt = f"{prompt:<{widths['prompt']}}"
    activity = _truncate_tail(activity, widths["output"])

    return " ".join(parts) + f" {prompt} | {activity}"


def _format_header(width, running, idle, total_tok_s, total_in, total_out):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_str = "-" if total_tok_s is None else f"{total_tok_s:.1f}"
    header = (
        f"codexapi top - {now}  running: {running}  idle: {idle}  "
        f"total tok/s: {total_str}"
    )
    if total_in is not None or total_out is not None:
        header += (
            f"  total in/out: {_format_token_total(total_in)}"
            f"/{_format_token_total(total_out)}"
        )
    return _truncate_head(header, width)


def _format_columns(layout):
    widths = layout["widths"]
    parts = []
    for key, align in layout["columns"]:
        title = _COLUMN_TITLES.get(key, key.upper())
        parts.append(f"{title:{align}{widths[key]}}")
    return " ".join(parts) + f" {'PROMPT':<{widths['prompt']}} | ACTIVITY"


def _print_top_help(width, show):
    def status(value):
        return "on" if value else "off"

    lines = [
        "codexapi top help",
        "",
        "Keys:",
        "  space  refresh",
        "  q / Esc  quit",
        "  h / ?  toggle help",
        f"  m  toggle MODEL column ({status(show.get('model'))})",
        f"  e  toggle EFF column ({status(show.get('effort'))})",
        f"  p  toggle PERM column ({status(show.get('perm'))})",
    ]
    for line in lines:
        print(_truncate_head(line, width))


def _print_top_once(show):
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    sessions = _active_sessions(root)
    width = shutil.get_terminal_size((160, 20)).columns

    if not sessions:
        print("No active Codex sessions.")
        return

    running = sum(1 for session in sessions if session.get("status") == "running")
    idle = sum(1 for session in sessions if session.get("status") == "idle")
    total_tok_s = sum(
        session["tok_s"]
        for session in sessions
        if session.get("status") == "running" and session["tok_s"] is not None
    )
    if total_tok_s == 0:
        total_tok_s = None
    total_input = 0
    total_output = 0
    have_input = False
    have_output = False
    for session in sessions:
        usage = session.get("total_usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int):
            total_input += input_tokens
            have_input = True
        if isinstance(output_tokens, int):
            total_output += output_tokens
            have_output = True

    max_depth = max(session.get("depth", 0) for session in sessions)
    layout = _layout_columns(width, 8 + max_depth, show)

    print(
        _format_header(
            width,
            running,
            idle,
            total_tok_s,
            total_input if have_input else None,
            total_output if have_output else None,
        )
    )
    print(_format_columns(layout))

    for session in sessions:
        print(_format_session(session, layout))


def _clean_foreach_list(path, retry_failed, retry_all):
    with open(path, "r", encoding="utf-8") as handle:
        data = handle.read()
    ends_with_newline = data.endswith("\n")
    lines = data.splitlines()

    cleaned = []
    changed = False
    for line in lines:
        new_line = line
        if retry_all or (retry_failed and new_line.startswith("❌")):
            if new_line and new_line[0] in _FOREACH_STATUS_MARKERS:
                new_line = new_line[1:]
                if new_line.startswith(" "):
                    new_line = new_line[1:]
            pipe = new_line.find("|")
            if pipe != -1:
                new_line = new_line[:pipe].rstrip()
        if new_line != line:
            changed = True
        cleaned.append(new_line)

    if not changed:
        return
    text = "\n".join(cleaned)
    if ends_with_newline:
        text += "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _run_top(argv):
    if argv and argv[0] in ("-h", "--help"):
        print("usage: codexapi top")
        return
    if argv:
        raise SystemExit("codexapi top takes no arguments.")
    if not sys.stdout.isatty():
        _print_top_once(
            {
                "model": False,
                "effort": False,
                "perm": False,
                "cwd": True,
            }
        )
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    show = {
        "model": False,
        "effort": False,
        "perm": False,
        "cwd": True,
    }
    show_help = False
    try:
        while True:
            sys.stdout.write("\033[H\033[J")
            width = shutil.get_terminal_size((160, 20)).columns
            if show_help:
                _print_top_help(width, show)
            else:
                _print_top_once(show)
            sys.stdout.flush()
            ready, _unused, _unused2 = select.select([sys.stdin], [], [], 1)
            if not ready:
                continue
            ch = sys.stdin.read(1)
            if ch in ("q", "\x1b"):
                break
            if ch in ("h", "?"):
                show_help = not show_help
                continue
            if show_help:
                show_help = False
            if ch == "m":
                show["model"] = not show["model"]
                continue
            if ch == "e":
                show["effort"] = not show["effort"]
                continue
            if ch == "p":
                show["perm"] = not show["perm"]
                continue
    except KeyboardInterrupt:
        return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ralph_help = (
        "Ralph loop mode (ralph command):\n"
        "  Repeats the exact same prompt each iteration until a completion promise\n"
        "  is detected or --max-iterations is reached (0 means unlimited).\n"
        "  Completion promise: output <promise>TEXT</promise> where TEXT matches\n"
        "  --completion-promise after trimming/collapsing whitespace. CRITICAL RULE:\n"
        "  Only output the promise when it is completely and unequivocally TRUE.\n"
        "  Welfare stop: the agent may stop early by outputting MAKE IT STOP as the\n"
        "  first non-empty line of its message.\n"
        "  Cancel by deleting .codexapi/ralph-loop.local.md or running codexapi ralph --cancel.\n"
        "  Default starts each iteration with a fresh Agent context; use --ralph-reuse\n"
        "  to reuse a single thread across iterations.\n"
    )
    science_help = (
        "Science mode (science command):\n"
        "  Wraps your short task in a science prompt and runs it via the Ralph loop.\n"
        "  Default uses --yolo. Use --no-yolo to disable it.\n"
        "  Optional --max-duration stops before starting the next iteration once\n"
        "  the duration limit is reached (e.g. 90m, 2h, 45s; default unit is minutes).\n"
    )
    parser = argparse.ArgumentParser(
        prog="codexapi",
        description="Run agent backends via the codexapi wrapper.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = _add_subparser(
        subparsers,
        "run",
        "Run an agent prompt.",
    )
    run_parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send. Use '-' or omit to read from stdin.",
    )
    run_parser.add_argument("--cwd", help="Working directory for the agent session.")
    run_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    run_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    run_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    run_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )
    run_parser.add_argument(
        "--include-thinking",
        action="store_true",
        help="Return all agent messages joined together (Codex only).",
    )

    lead_parser = _add_subparser(
        subparsers,
        "lead",
        "Periodically check in to lead long-running work.",
    )
    lead_parser.add_argument(
        "minutes",
        type=int,
        help="Check-in interval in minutes (integer, >= 0).",
    )
    lead_parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send. Use '-' or omit to read from stdin.",
    )
    lead_parser.add_argument(
        "-f",
        "--prompt-file",
        help="Read the lead prompt from a file.",
    )
    lead_parser.add_argument("--cwd", help="Working directory for the agent session.")
    lead_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    lead_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    lead_parser.add_argument(
        "--leadbook",
        help="Path to the leadbook file (default: LEADBOOK.md in cwd).",
    )
    lead_parser.add_argument(
        "--no-leadbook",
        action="store_true",
        help="Disable leadbook injection and checks.",
    )
    lead_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    lead_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )
    run_parser.add_argument(
        "--thread-id",
        help="Resume an existing thread/session id.",
    )
    run_parser.add_argument(
        "--print-thread-id",
        action="store_true",
        help="Print the current thread id to stderr after running.",
    )

    agent_parser = _add_subparser(
        subparsers,
        "agent",
        "Manage durable long-running agents.",
    )
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command")

    agent_start = _add_subparser(
        agent_subparsers,
        "start",
        "Create a durable agent and return immediately unless --wait is set.",
    )
    agent_start.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send. Use '-' or omit to read from stdin.",
    )
    agent_start.add_argument("--cwd", help="Working directory for the agent.")
    agent_start.add_argument("--name", help="Optional agent name.")
    agent_start.add_argument(
        "--created-by",
        help="Creator label (defaults to $USER).",
    )
    agent_start.add_argument(
        "--parent",
        help="Optional parent agent id, unique prefix, or name.",
    )
    agent_start.add_argument(
        "--stop-policy",
        default="until_done",
        choices=("until_done", "until_stopped"),
        help="Whether the agent stops itself when done or runs until stopped.",
    )
    agent_start.add_argument(
        "--heartbeat-minutes",
        type=int,
        default=5,
        help="Heartbeat interval in minutes (default: 5).",
    )
    agent_start.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    agent_start.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    agent_start.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    agent_start.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the backend.",
    )
    agent_start.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the first local wake to finish instead of just scheduling it.",
    )

    _add_subparser(
        agent_subparsers,
        "list",
        "List durable agents in this CODEXAPI_HOME.",
    )
    _add_subparser(
        agent_subparsers,
        "whoami",
        "Show the effective host and CODEXAPI_HOME for agents.",
    )

    agent_run = _add_subparser(
        agent_subparsers,
        "run",
        argparse.SUPPRESS,
    )
    agent_run.add_argument("agent_ref", help=argparse.SUPPRESS)

    agent_show = _add_subparser(
        agent_subparsers,
        "show",
        "Show one durable agent.",
    )
    agent_show.add_argument("agent_ref", help="Agent id, unique prefix, or name.")

    agent_status = _add_subparser(
        agent_subparsers,
        "status",
        "Show the latest rollout turn for one durable agent.",
    )
    agent_status.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
    agent_status.add_argument(
        "--actions",
        "--with-actions",
        action="store_true",
        dest="actions",
        help="Include verbose tool actions from the latest turn.",
    )

    agent_read = _add_subparser(
        agent_subparsers,
        "read",
        "Read recent visible communication for one agent.",
    )
    agent_read.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
    agent_read.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of items to show (default: 10).",
    )

    agent_book = _add_subparser(
        agent_subparsers,
        "book",
        "Show the current agentbook for one agent.",
    )
    agent_book.add_argument("agent_ref", help="Agent id, unique prefix, or name.")

    agent_send = _add_subparser(
        agent_subparsers,
        "send",
        "Queue a message for an agent and return immediately unless --wait is set.",
    )
    agent_send.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
    agent_send.add_argument("message", help="Message to queue.")
    agent_send.add_argument("--author", help="Author label for the message.")
    agent_send.add_argument(
        "--wait",
        action="store_true",
        help="Wait for a local wake after queueing the message.",
    )

    for subcommand, help_text in (
        ("wake", "Request an extra wake for an agent and return immediately unless --wait is set."),
        ("pause", "Pause an agent."),
        ("resume", "Resume a paused agent and return immediately unless --wait is set."),
        ("cancel", "Cancel an agent."),
    ):
        subparser = _add_subparser(agent_subparsers, subcommand, help_text)
        subparser.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
        subparser.add_argument("--author", help="Author label for the command.")
        if subcommand in ("wake", "resume"):
            subparser.add_argument(
                "--wait",
                action="store_true",
                help="Wait for a local wake after queueing the command.",
            )

    agent_recover = _add_subparser(
        agent_subparsers,
        "recover",
        "Terminate a stuck local wake, mark it recoverable, and optionally wait for a fresh wake.",
    )
    agent_recover.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
    agent_recover.add_argument(
        "--wait",
        action="store_true",
        help="Wait for a local wake after recovery.",
    )

    agent_set_heartbeat = _add_subparser(
        agent_subparsers,
        "set-heartbeat",
        "Update the heartbeat interval for one durable agent.",
    )
    agent_set_heartbeat.add_argument(
        "agent_ref",
        help="Agent id, unique prefix, or name.",
    )
    agent_set_heartbeat.add_argument(
        "heartbeat_minutes",
        type=int,
        help="Heartbeat interval in minutes.",
    )

    agent_delete = _add_subparser(
        agent_subparsers,
        "delete",
        "Delete one durable agent and its files.",
    )
    agent_delete.add_argument("agent_ref", help="Agent id, unique prefix, or name.")
    agent_delete.add_argument(
        "--force",
        action="store_true",
        help="Delete even when the agent is not terminal or still has children.",
    )

    _add_subparser(
        agent_subparsers,
        "tick",
        "Process due agents for the current host.",
    )
    _add_subparser(
        agent_subparsers,
        "install-cron",
        "Install or update the cron entry for this CODEXAPI_HOME.",
    )
    _add_subparser(
        agent_subparsers,
        "uninstall-cron",
        "Remove the cron entry for this CODEXAPI_HOME.",
    )

    _add_subparser(
        subparsers,
        "tick",
        "Run one full background tick.",
    )

    task_parser = _add_subparser(
        subparsers,
        "task",
        "Run a task with verification retries.",
    )
    task_parser.add_argument(
        "-f",
        "--task-file",
        help="YAML task file to run.",
    )
    task_parser.add_argument(
        "-i",
        "--item",
        help="Item value for task files that use {{item}} placeholders.",
    )
    task_parser.add_argument(
        "-p",
        "--project",
        help="When using -p, also pass -n agent_name TASK_FILE1 [TASK_FILE2 ...].",
    )
    task_parser.add_argument(
        "-s",
        "--status",
        default="Ready",
        help="Status name to take from when using --project (default: Ready).",
    )
    task_parser.add_argument(
        "-n",
        "--name",
        help="Owner label name for gh-task when using --project.",
    )
    task_parser.add_argument(
        "--only-matching",
        help=(
            "When using --project, only take issues whose title matches this regex. "
            "Useful for filtering tasks by hardware encoded in the issue title/path."
        ),
    )
    task_parser.add_argument(
        "task_args",
        nargs="*",
        help="Prompt to send (no --project) or task files (with --project).",
    )
    task_parser.add_argument(
        "--check",
        help="Optional check prompt. Defaults to the task prompt.",
    )
    task_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Max agent iterations (0 means unlimited). "
            f"Defaults to {DEFAULT_MAX_ITERATIONS}."
        ),
    )
    task_parser.add_argument("--cwd", help="Working directory for the agent session.")
    task_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    task_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    task_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    task_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )
    task_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output during verification.",
    )
    task_parser.add_argument(
        "--loop",
        action="store_true",
        help="With -p, keep taking tasks and wait when none are available.",
    )

    ralph_parser = _add_subparser(
        subparsers,
        "ralph",
        "Run a Ralph loop.",
        epilog=ralph_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ralph_parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send. Use '-' or omit to read from stdin.",
    )
    ralph_parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Max iterations for the loop (0 means unlimited).",
    )
    ralph_parser.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel the Ralph loop state in the target cwd.",
    )
    ralph_parser.add_argument(
        "--completion-promise",
        help="Promise text to match in <promise>...</promise>.",
    )
    ralph_fresh_group = ralph_parser.add_mutually_exclusive_group()
    ralph_fresh_group.add_argument(
        "--ralph-fresh",
        action="store_true",
        dest="ralph_fresh",
        default=None,
        help="Start each iteration with a fresh Agent context (default).",
    )
    ralph_fresh_group.add_argument(
        "--ralph-reuse",
        action="store_false",
        dest="ralph_fresh",
        default=None,
        help="Reuse the same Agent context each iteration.",
    )
    ralph_parser.add_argument("--cwd", help="Working directory for the agent session.")
    ralph_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    ralph_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    ralph_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    ralph_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )

    science_parser = _add_subparser(
        subparsers,
        "science",
        "Run a science-mode Ralph loop.",
        epilog=science_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    science_parser.add_argument(
        "task",
        nargs="?",
        help="Short task description. Use '-' or omit to read from stdin.",
    )
    science_parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Max iterations for the loop (0 means unlimited).",
    )
    science_parser.add_argument(
        "--max-duration",
        help=(
            "Maximum loop runtime. Stops after the current iteration when reached. "
            "Accepts s/m/h/d units (e.g. 90m, 2h, 45s); default unit is minutes."
        ),
    )
    science_parser.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel the Ralph loop state in the target cwd.",
    )
    science_parser.add_argument(
        "--completion-promise",
        help="Promise text to match in <promise>...</promise>.",
    )
    science_fresh_group = science_parser.add_mutually_exclusive_group()
    science_fresh_group.add_argument(
        "--ralph-fresh",
        action="store_true",
        dest="ralph_fresh",
        default=None,
        help="Start each iteration with a fresh Agent context (default).",
    )
    science_fresh_group.add_argument(
        "--ralph-reuse",
        action="store_false",
        dest="ralph_fresh",
        default=None,
        help="Reuse the same Agent context each iteration.",
    )
    science_parser.add_argument("--cwd", help="Working directory for the agent session.")
    science_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    science_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    science_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    science_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )

    foreach_parser = _add_subparser(
        subparsers,
        "foreach",
        "Run a task file over a list file.",
    )
    foreach_parser.add_argument(
        "list_file",
        help="Path to the list file to process.",
    )
    foreach_parser.add_argument(
        "task_file",
        help="Path to the YAML task file.",
    )
    foreach_retry_group = foreach_parser.add_mutually_exclusive_group()
    foreach_retry_group.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset failed (❌) items for re-run.",
    )
    foreach_retry_group.add_argument(
        "--retry-all",
        action="store_true",
        help="Reset all items for re-run.",
    )
    foreach_parser.add_argument(
        "-n",
        type=int,
        help="Limit parallelism to N.",
    )
    foreach_parser.add_argument("--cwd", help="Working directory for the agent session.")
    foreach_parser.add_argument(
        "--backend",
        choices=("codex", "cursor"),
        help="Agent backend to use (default: CODEXAPI_BACKEND or codex).",
    )
    foreach_parser.add_argument(
        "--fast",
        action="store_true",
        help="Use Codex fast mode (Codex backend only; default: normal mode).",
    )
    foreach_parser.add_argument(
        "--no-yolo",
        action="store_false",
        dest="yolo",
        help="Disable --yolo (Codex uses --full-auto).",
    )
    foreach_parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to the agent backend (quoted as needed).",
    )

    create_parser = _add_subparser(
        subparsers,
        "create",
        "Create a task file template.",
    )
    create_parser.add_argument(
        "filename",
        help="Filename for the new task file.",
    )

    reset_parser = _add_subparser(
        subparsers,
        "reset",
        "Reset project tasks back to Ready.",
    )
    reset_parser.add_argument(
        "-p",
        "--project",
        required=True,
        help="GitHub Project ref (owner/projects/3).",
    )
    reset_parser.add_argument(
        "-n",
        "--name",
        default="reset",
        help="Owner label name for gh-task (default: reset).",
    )
    reset_parser.add_argument(
        "-d",
        "--description",
        action="store_true",
        help="Remove any Progress section in the issue body.",
    )

    _add_subparser(
        subparsers,
        "top",
        "Show running Codex sessions.",
    )
    _add_subparser(
        subparsers,
        "limit",
        "Show Codex rate limits.",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        raise SystemExit(2)
    if args.command == "agent":
        if args.agent_command is None:
            agent_parser.print_help()
            raise SystemExit(2)
        if args.agent_command == "start":
            prompt = _read_prompt(args.prompt)
            try:
                result = start_managed_agent(
                    prompt,
                    args.cwd,
                    args.name,
                    args.created_by,
                    args.parent,
                    args.stop_policy,
                    args.heartbeat_minutes,
                    args.backend,
                    args.yolo,
                    args.flags,
                    fast=args.fast,
                )
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from None
            result["waited"] = bool(args.wait)
            if args.wait:
                result["nudge"] = nudge_agent(result["id"], wait=True)
            _warn_agent_scheduler_missing()
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.agent_command == "list":
            _print_managed_agent_list(list_managed_agents())
            return
        if args.agent_command == "whoami":
            _print_managed_agent_identity()
            return
        if args.agent_command == "run":
            print(json.dumps(run_managed_agent(args.agent_ref), indent=2, sort_keys=True))
            return
        if args.agent_command == "show":
            _print_managed_agent_show(show_managed_agent(args.agent_ref))
            return
        if args.agent_command == "status":
            _print_managed_agent_status(
                status_managed_agent(args.agent_ref, include_actions=args.actions),
                include_actions=args.actions,
            )
            return
        if args.agent_command == "read":
            if args.limit < 1:
                raise SystemExit("--limit must be >= 1.")
            _print_managed_agent_read(read_managed_agent(args.agent_ref, args.limit))
            return
        if args.agent_command == "book":
            _print_managed_agent_book(read_agentbook(args.agent_ref))
            return
        if args.agent_command == "send":
            result = send_agent(args.agent_ref, args.message, args.author)
            result["waited"] = bool(args.wait)
            result["nudge"] = nudge_agent(args.agent_ref, wait=bool(args.wait))
            if args.wait:
                reply_info = _send_reply_info(args.agent_ref, result["id"])
                if reply_info:
                    result.update(reply_info)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.agent_command in ("wake", "resume"):
            result = control_agent(
                args.agent_ref,
                args.agent_command,
                args.author,
            )
            result["waited"] = bool(args.wait)
            result["nudge"] = nudge_agent(args.agent_ref, wait=bool(args.wait))
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.agent_command in ("pause", "cancel"):
            result = control_agent(
                args.agent_ref,
                args.agent_command,
                args.author,
            )
            result["waited"] = False
            result["nudge"] = nudge_agent(args.agent_ref, wait=True)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.agent_command == "recover":
            result = recover_managed_agent(args.agent_ref)
            result["waited"] = bool(args.wait)
            if args.wait:
                result["nudge"] = nudge_agent(args.agent_ref, wait=True)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.agent_command == "set-heartbeat":
            if args.heartbeat_minutes < 0:
                raise SystemExit("heartbeat_minutes must be >= 0.")
            print(
                json.dumps(
                    set_agent_heartbeat(
                        args.agent_ref,
                        args.heartbeat_minutes,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if args.agent_command == "delete":
            print(
                json.dumps(
                    delete_managed_agent(args.agent_ref, args.force),
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if args.agent_command == "tick":
            print(json.dumps(tick_managed_agents(), indent=2, sort_keys=True))
            return
        if args.agent_command == "install-cron":
            print(json.dumps(install_agent_cron(), indent=2, sort_keys=True))
            return
        if args.agent_command == "uninstall-cron":
            print(json.dumps(uninstall_agent_cron(), indent=2, sort_keys=True))
            return
    if args.command == "tick":
        print(json.dumps(_system_tick(), indent=2, sort_keys=True))
        return
    if args.command == "create":
        _create_task_template(args.filename)
        return
    if args.command == "reset":
        from .gh_integration import reset_project_tasks

        issues = reset_project_tasks(args.project, args.name, args.description)
        for issue in issues:
            title = (issue.title or "Untitled issue").strip()
            print(f"{issue.repo}#{issue.number} {title}")
        print(f"Reset {len(issues)} task(s).")
        return
    if args.command == "top":
        _run_top([])
        return
    if args.command == "limit":
        print(quota_line())
        return

    if args.command == "foreach":
        if args.n is not None and args.n < 1:
            raise SystemExit("-n must be >= 1.")
        if args.retry_failed or args.retry_all:
            _clean_foreach_list(
                args.list_file,
                args.retry_failed,
                args.retry_all,
            )
        result = foreach(
            args.list_file,
            args.task_file,
            args.n,
            args.cwd,
            args.yolo,
            args.flags,
            args.backend,
            args.fast,
        )
        if result.failed:
            raise SystemExit(1)
        return

    if args.command == "ralph":
        if args.cancel:
            if args.prompt:
                raise SystemExit("ralph --cancel takes no prompt.")
            if args.completion_promise or args.ralph_fresh is not None:
                raise SystemExit(
                    "--completion-promise/--ralph-fresh/--ralph-reuse are not allowed with --cancel."
                )
            if args.max_iterations != 0:
                raise SystemExit("--max-iterations is not allowed with --cancel.")
            print(cancel_ralph_loop(args.cwd))
            return
        if args.ralph_fresh is None:
            args.ralph_fresh = True
    if args.command == "science":
        if args.cancel:
            if args.task:
                raise SystemExit("science --cancel takes no task.")
            if args.completion_promise or args.ralph_fresh is not None:
                raise SystemExit(
                    "--completion-promise/--ralph-fresh/--ralph-reuse are not allowed with --cancel."
                )
            if args.max_iterations != 0:
                raise SystemExit("--max-iterations is not allowed with --cancel.")
            if args.max_duration:
                raise SystemExit("--max-duration is not allowed with --cancel.")
            print(cancel_ralph_loop(args.cwd))
            return
        if args.ralph_fresh is None:
            args.ralph_fresh = True

    if args.command == "task" and args.project:
        if args.task_file:
            raise SystemExit("task --project does not allow -f.")
        if args.item is not None:
            raise SystemExit("--item is only supported with -f.")
        if args.check is not None:
            raise SystemExit("--check is not allowed with --project.")
        if args.max_iterations is not None:
            raise SystemExit("--max-iterations is not allowed with --project.")
        if not args.name:
            raise SystemExit("--name is required with --project.")
        if not args.task_args:
            raise SystemExit("task --project requires one or more task files.")
        if args.only_matching is not None:
            try:
                re.compile(args.only_matching)
            except re.error as exc:
                raise SystemExit(f"--only-matching regex is invalid: {exc}") from None
        from .gh_integration import GhTaskRunner, project_url
        from gh_task.errors import TakeError

        if args.loop:
            while True:
                try:
                    task_runner = GhTaskRunner(
                        args.project,
                        args.name,
                        args.task_args,
                        args.status,
                        args.only_matching,
                        args.cwd,
                        args.yolo,
                        args.flags,
                        args.backend,
                        args.fast,
                    )
                except TakeError as exc:
                    print(str(exc), file=sys.stderr)
                    print(
                        f"Waiting {_PROJECT_LOOP_SLEEP}s for new tasks...",
                        file=sys.stderr,
                    )
                    time.sleep(_PROJECT_LOOP_SLEEP)
                    continue
                if not args.quiet:
                    title = task_runner.issue_title or "Untitled issue"
                    print(
                        f"Task {task_runner.task_name}: {title} on {project_url(task_runner.project)}"
                    )
                result = task_runner(progress=not args.quiet)
                if not result.success:
                    raise SystemExit(1)
        else:
            try:
                task_runner = GhTaskRunner(
                    args.project,
                    args.name,
                    args.task_args,
                    args.status,
                    args.only_matching,
                    args.cwd,
                    args.yolo,
                    args.flags,
                    args.backend,
                    args.fast,
                )
            except TakeError as exc:
                raise SystemExit(str(exc)) from None
            if not args.quiet:
                title = task_runner.issue_title or "Untitled issue"
                print(
                    f"Task {task_runner.task_name}: {title} on {project_url(task_runner.project)}"
                )
            result = task_runner(progress=not args.quiet)
            if not result.success:
                raise SystemExit(1)
            return

    if args.command == "task" and args.task_file:
        if args.task_args:
            raise SystemExit("task -f does not take a prompt.")
        if args.item is not None:
            task_def = load_task_file(args.task_file)
            if not task_def_uses_item(task_def):
                raise SystemExit(
                    "task -f --item requires {{item}} in the task file."
                )
        if args.only_matching is not None:
            raise SystemExit("--only-matching is only supported with --project.")
        if args.check is not None:
            raise SystemExit("--check is not allowed with -f.")
        if args.max_iterations is not None:
            raise SystemExit("--max-iterations is not allowed with -f.")
        task_runner = TaskFile(
            args.task_file,
            args.item,
            cwd=args.cwd,
            yolo=args.yolo,
            thread_id=None,
            flags=args.flags,
            backend=args.backend,
            fast=args.fast,
        )
        result = task_runner(progress=not args.quiet)
        if not result.success:
            raise SystemExit(1)
        return

    prompt_source = None
    prompt = None
    if args.command in ("run", "ralph", "lead"):
        if args.command == "lead" and args.prompt_file:
            if args.prompt is not None:
                raise SystemExit("lead --prompt-file cannot be used with a prompt arg.")
            prompt = _read_prompt_file(args.prompt_file)
        else:
            prompt_source = args.prompt
    elif args.command == "science":
        prompt_source = args.task
    if args.command != "task" and prompt is None:
        prompt = _read_prompt(prompt_source)
    exit_code = 0
    message = None

    if args.command == "ralph":
        if args.max_iterations < 0:
            raise SystemExit("--max-iterations must be >= 0.")
        Ralph(
            prompt,
            args.cwd,
            args.yolo,
            args.flags,
            args.max_iterations,
            args.completion_promise,
            args.ralph_fresh,
            args.backend,
            args.fast,
        )()
        return
    if args.command == "science":
        if args.max_iterations < 0:
            raise SystemExit("--max-iterations must be >= 0.")
        max_duration_seconds = _parse_duration_seconds(args.max_duration, "--max-duration")
        Science(
            prompt,
            args.cwd,
            args.yolo,
            args.flags,
            args.max_iterations,
            args.completion_promise,
            args.ralph_fresh,
            max_duration_seconds,
            args.backend,
            args.fast,
        )()
        return
    if args.command == "lead":
        if args.minutes < 0:
            raise SystemExit("lead minutes must be >= 0.")
        try:
            if args.no_leadbook and args.leadbook:
                raise SystemExit("--leadbook and --no-leadbook are mutually exclusive.")
            leadbook = False if args.no_leadbook else args.leadbook
            lead(
                args.minutes,
                prompt,
                args.cwd,
                args.yolo,
                args.flags,
                leadbook,
                args.backend,
                args.fast,
            )
        except KeyboardInterrupt:
            raise SystemExit(130)
        except Exception as exc:
            raise SystemExit(str(exc) or "lead failed") from None
        return
    if args.command == "task":
        if args.project:
            raise SystemExit("task --project already handled earlier.")
        if args.loop:
            raise SystemExit("--loop is only supported with -p.")
        if args.item is not None:
            raise SystemExit("--item is only supported with -f.")
        if args.only_matching is not None:
            raise SystemExit("--only-matching is only supported with --project.")
        if args.max_iterations is None:
            args.max_iterations = DEFAULT_MAX_ITERATIONS
        if args.max_iterations < 0:
            raise SystemExit("--max-iterations must be >= 0.")
        check = args.check
        try:
            task_args = args.task_args or []
            if len(task_args) > 1:
                raise SystemExit("task takes a single prompt unless --project is used.")
            if task_args:
                prompt_source = task_args[0]
            prompt = _read_prompt(prompt_source)
            task(
                prompt,
                check,
                args.max_iterations,
                args.cwd,
                args.yolo,
                args.flags,
                not args.quiet,
                backend=args.backend,
                fast=args.fast,
            )
        except TaskFailed as exc:
            exit_code = 1
    else:
        use_session = args.thread_id or args.print_thread_id
        if use_session:
            session = Agent(
                args.cwd,
                args.yolo,
                args.thread_id,
                args.flags,
                include_thinking=args.include_thinking,
                backend=args.backend,
                fast=args.fast,
            )
            message = session(prompt)
            if args.print_thread_id:
                print(f"thread_id={session.thread_id}", file=sys.stderr)
        else:
            message = agent(
                prompt,
                args.cwd,
                args.yolo,
                args.flags,
                args.include_thinking,
                args.backend,
                fast=args.fast,
            )

    if message is not None:
        print(message)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
