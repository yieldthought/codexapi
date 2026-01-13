"""Codex CLI wrapper used by the codexapi public interface."""

import json
import os
import shlex
import subprocess

_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")


def agent(prompt, cwd=None, yolo=True, flags=None):
    """Run a single Codex turn and return only the agent's message.

    Args:
        prompt: The user prompt to send to Codex.
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        flags: Additional raw CLI flags to pass to Codex.

    Returns:
        The agent's visible response text with reasoning traces removed.
    """
    message, _thread_id = _run_codex(prompt, cwd, None, yolo, flags)
    return message


class Agent:
    """Stateful Codex session wrapper that resumes the same conversation.

    Example:
        session = Agent()
        first = session("Say hi")
        follow_up = session("What did you just say?")
    """

    def __init__(
        self,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        """Create a new session wrapper.

        Args:
            cwd: Optional working directory for the Codex session.
            yolo: Whether to pass --yolo to Codex.
            agent: Agent backend to use (only "codex" is supported).
            trace_id: Optional Codex thread id to resume from the first call.
            flags: Additional raw CLI flags to pass to Codex.
        """
        self.cwd = cwd
        self._yolo = yolo
        self._flags = flags
        self.thread_id = thread_id

    def __call__(self, prompt):
        """Send a prompt to Codex and return only the agent's message."""
        message, thread_id = _run_codex(
            prompt,
            self.cwd,
            self.thread_id,
            self._yolo,
            self._flags,
        )
        if thread_id:
            self.thread_id = thread_id
        return message


def _run_codex(prompt, cwd, thread_id, yolo, flags):
    """Invoke the Codex CLI and return the message plus thread id (if any)."""
    command = [
        _CODEX_BIN,
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
    ]
    if yolo:
        command.append("--yolo")
    else:
        command.append("--full-auto")
    if flags:
        command.extend(shlex.split(flags))
    if cwd:
        command.extend(["--cd", os.fspath(cwd)])
    if thread_id:
        command.extend(["resume", thread_id, "-"])
    else:
        command.append("-")

    result = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=os.fspath(cwd) if cwd else None,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        msg = f"Codex failed with exit code {result.returncode}."
        if stderr:
            msg = f"{msg}\n{stderr}"
        raise RuntimeError(msg)

    return _parse_jsonl(result.stdout)


def _parse_jsonl(output):
    """Extract agent messages and the latest thread id from Codex JSONL output."""
    thread_id = None
    messages = []
    raw_lines = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(line)
            continue

        if event.get("type") == "thread.started":
            maybe_thread = event.get("thread_id")
            if isinstance(maybe_thread, str):
                thread_id = maybe_thread

        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)

    if not messages:
        fallback = "\n".join(raw_lines) if raw_lines else output.strip()
        raise RuntimeError(
            "Codex returned no agent message. Raw output:\n" + fallback
        )

    return "\n\n".join(messages), thread_id
