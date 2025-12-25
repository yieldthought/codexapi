"""Codex CLI wrapper used by the codexapi public interface."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Optional, Tuple, Union

Pathish = Union[str, os.PathLike]

_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")


def agent(
    prompt: str,
    cwd: Optional[Pathish] = None,
    *,
    yolo: bool = False,
    agent: str = "codex",
    flags: Optional[str] = None,
) -> str:
    """Run a single Codex turn and return only the agent's message.

    Args:
        prompt: The user prompt to send to Codex.
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        agent: Agent backend to use (only "codex" is supported).
        flags: Additional raw CLI flags to pass to Codex.

    Returns:
        The agent's visible response text with reasoning traces removed.
    """
    _require_codex_agent(agent)
    message, _thread_id = _run_codex(
        prompt=prompt,
        cwd=cwd,
        thread_id=None,
        yolo=yolo,
        flags=flags,
    )
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
        cwd: Optional[Pathish] = None,
        *,
        yolo: bool = False,
        agent: str = "codex",
        trace_id: Optional[str] = None,
        flags: Optional[str] = None,
    ) -> None:
        """Create a new session wrapper.

        Args:
            cwd: Optional working directory for the Codex session.
            yolo: Whether to pass --yolo to Codex.
            agent: Agent backend to use (only "codex" is supported).
            trace_id: Optional Codex thread id to resume from the first call.
            flags: Additional raw CLI flags to pass to Codex.
        """
        _require_codex_agent(agent)
        self._cwd = cwd
        self._yolo = yolo
        self._flags = flags
        self._thread_id: Optional[str] = trace_id

    def __call__(self, prompt: str) -> str:
        """Send a prompt to Codex and return only the agent's message."""
        message, thread_id = _run_codex(
            prompt=prompt,
            cwd=self._cwd,
            thread_id=self._thread_id,
            yolo=self._yolo,
            flags=self._flags,
        )
        if thread_id:
            self._thread_id = thread_id
        return message

    @property
    def thread_id(self) -> Optional[str]:
        """Return the current Codex session id, if any."""
        return self._thread_id


def _run_codex(
    *,
    prompt: str,
    cwd: Optional[Pathish],
    thread_id: Optional[str],
    yolo: bool,
    flags: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Invoke the Codex CLI and return the message plus thread id (if any)."""
    cmd = [
        _CODEX_BIN,
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
    ]
    if yolo:
        cmd.append("--yolo")
    if flags:
        cmd.extend(shlex.split(flags))
    if cwd is not None:
        cmd.extend(["--cd", os.fspath(cwd)])
    if thread_id:
        cmd.extend(["resume", thread_id, "-"])
    else:
        cmd.append("-")

    result = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=os.fspath(cwd) if cwd is not None else None,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        msg = f"Codex failed with exit code {result.returncode}."
        if stderr:
            msg = f"{msg}\n{stderr}"
        raise RuntimeError(msg)

    return _parse_jsonl(result.stdout)


def _require_codex_agent(agent_name: str) -> None:
    if agent_name != "codex":
        raise ValueError('Only agent="codex" is supported right now.')


def _parse_jsonl(output: str) -> Tuple[str, Optional[str]]:
    """Extract agent messages and the latest thread id from Codex JSONL output."""
    thread_id: Optional[str] = None
    messages: list[str] = []
    raw_lines: list[str] = []

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
