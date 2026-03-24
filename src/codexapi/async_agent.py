"""Async wrapper for running agent backends without the durable registry."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid

from .agent import (
    _CODEX_BIN,
    _CURSOR_BIN,
    _ensure_backend_available,
    _event_usage,
    _merged_env,
    _normalize_usage,
    _parse_cursor_json,
    _resolve_backend,
)
from .agents import _last_rollout_turn, _resolve_rollout_path, _rollout_events

_TERMINAL_STATES = {"done", "error", "canceled"}


class AsyncAgent:
    """Run one agent call in a background subprocess and poll live progress."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        cwd: str | None,
        backend: str,
        name: str | None,
        include_thinking: bool,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.name = name or f"async-{self.id[:8]}"
        self.cwd = os.fspath(cwd) if cwd else os.getcwd()
        self.backend = backend
        self.include_thinking = include_thinking
        self.pid = process.pid

        self._process = process
        self._lock = threading.Lock()
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._messages: list[str] = []
        self._thread_id = ""
        self._rollout_path = ""
        self._progress: list[str] = []
        self._tools: list[dict[str, object]] = []
        self._last_event_at = ""
        self._rollout_final_output = ""
        self._last_usage: dict[str, int] = {}
        self._stdout_done = False
        self._stderr_done = False
        self._cursor_parsed = False
        self._canceled = False

        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name=f"codexapi-async-stdout-{self.id[:8]}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name=f"codexapi-async-stderr-{self.id[:8]}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def start(
        cls,
        prompt,
        cwd=None,
        yolo=True,
        flags=None,
        include_thinking=False,
        backend=None,
        env=None,
        name=None,
    ):
        """Start a backend subprocess and return an async handle immediately."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        backend = _resolve_backend(backend)
        _ensure_backend_available(backend, env)
        command = _build_command(backend, cwd, yolo, flags)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=os.fspath(cwd) if cwd else None,
            env=_merged_env(env),
        )
        agent = cls(
            process,
            cwd=cwd,
            backend=backend,
            name=name,
            include_thinking=include_thinking,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            if not prompt.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.close()
        except Exception:
            agent.cancel()
            raise
        return agent

    @property
    def thread_id(self) -> str:
        with self._lock:
            return self._thread_id

    @property
    def last_usage(self) -> dict[str, int]:
        with self._lock:
            return dict(self._last_usage)

    def show(self) -> dict[str, object]:
        """Return a concise snapshot of the current local async run."""
        status = self.status()
        return {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "backend": self.backend,
            "pid": self.pid,
            "thread_id": status["thread_id"],
            "status": status["status"],
            "activity": status["activity"],
            "returncode": status["returncode"],
        }

    def status(self, include_actions=False) -> dict[str, object]:
        """Return the current process and rollout snapshot."""
        self._refresh_rollout()
        self._finalize_cursor_output()
        with self._lock:
            returncode = self._process.poll()
            status = _status_text(returncode, self._canceled)
            final_output = self._current_final_output_locked()
            progress = list(self._progress)
            tools = list(self._tools) if include_actions else []
            stderr_lines = list(self._stderr_lines)
            thread_id = self._thread_id
            rollout_path = self._rollout_path
            last_event_at = self._last_event_at
            last_usage = dict(self._last_usage)
            messages = list(self._messages)

        activity = _activity_text(
            status=status,
            progress=progress,
            final_output=final_output,
            stderr_lines=stderr_lines,
        )
        return {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "backend": self.backend,
            "pid": self.pid,
            "status": status,
            "activity": activity,
            "thread_id": thread_id,
            "rollout_path": rollout_path,
            "progress": progress,
            "tools": tools,
            "final_output": final_output,
            "last_event_at": last_event_at,
            "returncode": returncode,
            "last_error": stderr_lines[-1] if stderr_lines else "",
            "stderr": "\n".join(stderr_lines),
            "messages": messages,
            "usage": last_usage,
        }

    def watch(self, poll_interval=2.0, timeout=None, include_actions=False):
        """Yield changed snapshots until the subprocess fully exits."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")

        started = time.monotonic()
        last_key = None
        while True:
            snapshot = self.status(include_actions=include_actions)
            key = (
                snapshot["status"],
                snapshot["thread_id"],
                len(snapshot["progress"]),
                len(snapshot["tools"]),
                snapshot["last_event_at"],
                snapshot["final_output"],
                snapshot["returncode"],
            )
            if key != last_key:
                yield snapshot
                last_key = key

            if snapshot["status"] in _TERMINAL_STATES and self._io_drained():
                return
            if timeout is not None and (time.monotonic() - started) >= timeout:
                return
            time.sleep(poll_interval)

    def wait(self, poll_interval=2.0, timeout=None, include_actions=False):
        """Poll until the subprocess exits and return the final snapshot."""
        last = None
        for update in self.watch(
            poll_interval=poll_interval,
            timeout=timeout,
            include_actions=include_actions,
        ):
            last = update
        return last or self.status(include_actions=include_actions)

    def cancel(self, terminate_timeout=2.0, kill_timeout=2.0) -> None:
        """Stop the subprocess if it is still running."""
        with self._lock:
            self._canceled = True
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=terminate_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self._process.kill()
        try:
            self._process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired:
            pass

    def _io_drained(self) -> bool:
        with self._lock:
            return self._stdout_done and self._stderr_done

    def _read_stdout(self) -> None:
        handle = self._process.stdout
        try:
            if handle is None:
                return
            for raw_line in handle:
                line = raw_line.rstrip("\r\n")
                with self._lock:
                    self._stdout_lines.append(line)
                self._handle_stdout_line(line)
        finally:
            if handle is not None:
                handle.close()
            with self._lock:
                self._stdout_done = True

    def _read_stderr(self) -> None:
        handle = self._process.stderr
        try:
            if handle is None:
                return
            for raw_line in handle:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                with self._lock:
                    self._stderr_lines.append(line)
        finally:
            if handle is not None:
                handle.close()
            with self._lock:
                self._stderr_done = True

    def _handle_stdout_line(self, line: str) -> None:
        if not line:
            return
        if self.backend == "cursor":
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        usage = _stream_event_usage(event)
        with self._lock:
            if usage:
                self._last_usage = usage
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str):
                    self._thread_id = thread_id
            elif event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        self._messages.append(text)

    def _refresh_rollout(self) -> None:
        if self.backend != "codex":
            return
        with self._lock:
            thread_id = self._thread_id
            known_path = self._rollout_path
        if not thread_id:
            return
        rollout_path = _resolve_rollout_path(known_path, thread_id)
        if rollout_path is None or not rollout_path.exists():
            return
        turn = _last_rollout_turn(_rollout_events(rollout_path), include_actions=True)
        with self._lock:
            self._rollout_path = str(rollout_path)
            if turn is not None:
                self._progress = turn.get("progress") or []
                self._tools = turn.get("tools") or []
                self._last_event_at = turn.get("last_event_at") or ""
                self._rollout_final_output = turn.get("final_output") or ""

    def _finalize_cursor_output(self) -> None:
        if self.backend != "cursor":
            return
        with self._lock:
            if self._cursor_parsed or not self._stdout_done:
                return
            output = "\n".join(self._stdout_lines)
        try:
            message, thread_id, usage = _parse_cursor_json(output, self.include_thinking)
        except Exception as exc:
            with self._lock:
                self._stderr_lines.append(str(exc))
                self._cursor_parsed = True
            return
        with self._lock:
            self._messages = [message]
            self._thread_id = thread_id or ""
            self._last_usage = usage or {}
            self._cursor_parsed = True

    def _current_final_output_locked(self) -> str:
        if self._messages:
            if self.include_thinking:
                return "\n\n".join(self._messages)
            return self._messages[-1]
        return self._rollout_final_output


def _build_command(backend, cwd, yolo, flags):
    if backend == "codex":
        return _build_codex_command(cwd, yolo, flags)
    return _build_cursor_command(cwd, yolo, flags)


def _build_codex_command(cwd, yolo, flags):
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
    command.append("-")
    return command


def _build_cursor_command(cwd, yolo, flags):
    command = [
        _CURSOR_BIN,
        "agent",
        "--trust",
    ]
    if cwd:
        command.extend(["--workspace", os.fspath(cwd)])
    if yolo:
        command.append("--yolo")
    if flags:
        command.extend(shlex.split(flags))
    command.extend(["--print", "--output-format", "json"])
    return command


def _stream_event_usage(event):
    usage = _event_usage(event)
    if usage:
        return usage
    if not isinstance(event, dict):
        return {}
    if event.get("type") == "turn.completed":
        payload = event.get("usage")
        if isinstance(payload, dict):
            return _normalize_usage(payload)
    return {}


def _status_text(returncode, canceled):
    if returncode is None:
        return "running"
    if canceled:
        return "canceled"
    if returncode == 0:
        return "done"
    return "error"


def _activity_text(status, progress, final_output, stderr_lines):
    if progress:
        return progress[-1]
    if status == "error" and stderr_lines:
        return stderr_lines[-1]
    if final_output:
        return final_output
    if status == "done":
        return "Finished"
    if status == "canceled":
        return "Canceled"
    return "Running"
