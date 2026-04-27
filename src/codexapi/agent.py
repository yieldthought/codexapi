"""Agent CLI wrapper used by the codexapi public interface."""

import json
import os
import shlex
import shutil
import subprocess

from . import welfare

_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
_CURSOR_BIN = os.environ.get("CURSOR_BIN", "cursor")
_SUPPORTED_BACKENDS = {"codex", "cursor"}
_CURSOR_AGENT_BIN = os.path.expanduser("~/.local/bin/cursor-agent")


def _resolve_backend(backend):
    if backend is None:
        backend = os.environ.get("CODEXAPI_BACKEND", "codex")
    if not isinstance(backend, str):
        raise TypeError("backend must be a string")
    backend = backend.strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        choices = ", ".join(sorted(_SUPPORTED_BACKENDS))
        raise ValueError(f"Unknown backend '{backend}'. Choose one of: {choices}.")
    return backend


def _ensure_backend_available(backend, env=None):
    """Return the resolved backend executable or raise when it is unavailable."""
    backend = _resolve_backend(backend)
    if backend == "codex":
        command = _CODEX_BIN
        env_var = "CODEX_BIN"
        label = "Codex CLI"
    else:
        command = _cursor_bin(env)
        env_var = "CURSOR_BIN"
        label = "Cursor agent CLI"
    merged = _merged_env(env)
    path_value = None if merged is None else merged.get("PATH")
    if os.path.isabs(command):
        resolved = command if os.path.exists(command) else None
    else:
        resolved = shutil.which(command, path=path_value)
    if resolved:
        return resolved
    raise RuntimeError(
        f"{label} not found: {command!r}. Install it or set {env_var} to an executable on PATH."
    )


def agent(
    prompt,
    cwd=None,
    yolo=True,
    flags=None,
    include_thinking=False,
    backend=None,
    env=None,
    fast=False,
    model=None,
    thinking=None,
):
    """Run a single agent turn and return only the agent's message.

    Args:
        prompt: The user prompt to send to the agent backend.
        cwd: Optional working directory for the agent session.
        yolo: Whether to pass --yolo to the agent backend.
        flags: Additional raw CLI flags to pass to the agent backend.
        include_thinking: When true, return all agent messages joined together.
        backend: Agent backend to use ("codex" or "cursor").
        env: Optional environment variables for the backend subprocess.
        fast: Enable Codex fast mode. Defaults to normal mode.
        model: Optional backend model override.
        thinking: Optional backend reasoning/thinking effort override.

    Returns:
        The agent's visible response text with reasoning traces removed.
    """
    message, _thread_id, _usage = _run_agent(
        prompt,
        cwd,
        None,
        yolo,
        flags,
        include_thinking,
        backend,
        env,
        fast,
        model,
        thinking,
    )
    return message


class WelfareStop(RuntimeError):
    """Raised when an agent requests an early stop via the welfare sentinel."""

    def __init__(self, agent_message):
        super().__init__("Agent requested stop via welfare sentinel.")
        self.agent_message = agent_message
        self.note = welfare.stop_note(agent_message)


class Agent:
    """Stateful session wrapper that resumes the same conversation.

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
        welfare=False,
        include_thinking=False,
        backend=None,
        env=None,
        fast=False,
        model=None,
        thinking=None,
    ):
        """Create a new session wrapper.

        Args:
            cwd: Optional working directory for the agent session.
            yolo: Whether to pass --yolo to the agent backend.
            thread_id: Optional thread/session id to resume from the first call.
            flags: Additional raw CLI flags to pass to the agent backend.
            welfare: When true, append welfare stop instructions to each prompt
                and raise WelfareStop if the agent outputs MAKE IT STOP.
            include_thinking: When true, return all agent messages joined together.
            backend: Agent backend to use ("codex" or "cursor").
            env: Optional environment variables for the backend subprocess.
            fast: Enable Codex fast mode. Defaults to normal mode.
            model: Optional backend model override.
            thinking: Optional backend reasoning/thinking effort override.
        """
        self.cwd = cwd
        self._yolo = yolo
        self._flags = flags
        self._welfare = welfare
        self._include_thinking = include_thinking
        self.thread_id = thread_id
        self._backend = backend
        self._env = env
        self._fast = fast
        self._model = model
        self._thinking = thinking
        self.last_usage = {}

    def __call__(self, prompt):
        """Send a prompt to the agent backend and return the message."""
        if self._welfare:
            prompt = welfare.append_instructions(prompt)
        message, thread_id, usage = _run_agent(
            prompt,
            self.cwd,
            self.thread_id,
            self._yolo,
            self._flags,
            self._include_thinking,
            self._backend,
            self._env,
            self._fast,
            self._model,
            self._thinking,
        )
        if thread_id:
            self.thread_id = thread_id
        self.last_usage = usage or {}
        if self._welfare and welfare.stop_requested(message):
            raise WelfareStop(message)
        return message


def _run_agent(
    prompt,
    cwd,
    thread_id,
    yolo,
    flags,
    include_thinking,
    backend,
    env,
    fast=False,
    model=None,
    thinking=None,
):
    backend = _resolve_backend(backend)
    _ensure_backend_available(backend, env)
    if backend == "codex":
        return _run_codex(
            prompt,
            cwd,
            thread_id,
            yolo,
            flags,
            include_thinking,
            env,
            fast,
            model,
            thinking,
        )
    return _run_cursor(
        prompt,
        cwd,
        thread_id,
        yolo,
        flags,
        include_thinking,
        env,
        model,
        thinking,
    )


def _run_codex(
    prompt,
    cwd,
    thread_id,
    yolo,
    flags,
    include_thinking,
    env,
    fast=False,
    model=None,
    thinking=None,
):
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
    command.extend(_codex_fast_config(fast))
    command.extend(_agent_config_flag_parts("codex", model, thinking))
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
        env=_merged_env(env),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        msg = f"Codex failed with exit code {result.returncode}."
        if stderr:
            msg = f"{msg}\n{stderr}"
        raise RuntimeError(msg)

    return _parse_jsonl(result.stdout, include_thinking)


def _codex_fast_config(fast):
    """Return Codex config flags for normal or fast mode."""
    if fast:
        return [
            "-c",
            "service_tier=fast",
            "-c",
            "features.fast_mode=true",
        ]
    return ["-c", "features.fast_mode=false"]


def _cursor_bin(env=None):
    merged = _merged_env(env)
    env_source = merged or os.environ
    override = env_source.get("CURSOR_BIN", "").strip()
    if override:
        return os.path.expanduser(override)

    path_value = None if merged is None else merged.get("PATH")
    direct = shutil.which("cursor-agent", path=path_value)
    if direct:
        return direct
    if os.path.exists(_CURSOR_AGENT_BIN):
        return _CURSOR_AGENT_BIN
    return _CURSOR_BIN


def _cursor_command_prefix(env=None):
    command = _cursor_bin(env)
    if os.path.basename(command) == "cursor-agent":
        return [command]
    return [command, "agent"]


def build_agent_flags(*, backend=None, model=None, thinking=None, flags=None):
    """Return raw backend flags for a model/thinking configuration.

    The returned string is suitable for APIs that accept the existing ``flags``
    parameter.
    """
    backend = _resolve_backend(backend)
    parts = _agent_config_flag_parts(backend, model, thinking)
    if flags:
        parts.extend(shlex.split(flags))
    return shlex.join(parts)


def _agent_config_flag_parts(backend, model=None, thinking=None):
    backend = _resolve_backend(backend)
    parts = []
    model = _clean_optional_text(model)
    thinking = _clean_optional_text(thinking)

    if backend == "codex":
        if model:
            parts.extend(["-c", f"model={model}"])
        if thinking:
            parts.extend(["-c", f"model_reasoning_effort={thinking}"])
        return parts

    if model:
        parts.extend(["--model", model])
    if thinking:
        raise ValueError("thinking is only supported by the codex backend")
    return parts


def _clean_optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_cursor(
    prompt,
    cwd,
    thread_id,
    yolo,
    flags,
    include_thinking,
    env,
    model=None,
    thinking=None,
):
    """Invoke the Cursor agent CLI and return the message plus session id (if any)."""
    command = _cursor_command_prefix(env) + [
        "--trust",
    ]
    if cwd:
        command.extend(["--workspace", os.fspath(cwd)])
    if thread_id:
        command.extend(["--resume", thread_id])
    if yolo:
        command.append("--yolo")
    command.extend(_agent_config_flag_parts("cursor", model, thinking))
    if flags:
        command.extend(shlex.split(flags))
    command.extend(["--print", "--output-format", "json"])

    result = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=os.fspath(cwd) if cwd else None,
        env=_merged_env(env),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        msg = f"Cursor agent failed with exit code {result.returncode}."
        if stderr:
            msg = f"{msg}\n{stderr}"
        raise RuntimeError(msg)

    return _parse_cursor_json(result.stdout, include_thinking)


def _parse_jsonl(output, include_thinking):
    """Extract agent messages and the latest thread id from Codex JSONL output."""
    thread_id = None
    messages = []
    raw_lines = []
    usage = {}

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

        maybe_usage = _event_usage(event)
        if maybe_usage:
            usage = maybe_usage

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

    if include_thinking:
        return "\n\n".join(messages), thread_id, usage
    return messages[-1], thread_id, usage


def _parse_cursor_json(output, include_thinking):
    """Extract the agent message and session id from Cursor JSON output.

    Cursor returns a single result string; include_thinking has no effect.
    """
    payload = None
    raw_lines = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(line)
            continue
        if isinstance(decoded, dict):
            if "result" in decoded:
                payload = decoded
            elif payload is None:
                payload = decoded

    if payload is None:
        fallback = "\n".join(raw_lines) if raw_lines else output.strip()
        raise RuntimeError(
            "Cursor returned no JSON output. Raw output:\n" + fallback
        )

    if payload.get("is_error"):
        message = payload.get("result")
        if not isinstance(message, str) or not message.strip():
            message = "Cursor returned an error response."
        raise RuntimeError(message)

    result = payload.get("result")
    if not isinstance(result, str):
        fallback = output.strip()
        raise RuntimeError(
            "Cursor returned no result text. Raw output:\n" + fallback
        )

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None
    return result, session_id, {}


def _merged_env(env):
    """Return subprocess env overlaying the current process env."""
    if env is None:
        return None
    if not isinstance(env, dict):
        raise TypeError("env must be a dict or None")
    merged = os.environ.copy()
    for key, value in env.items():
        if value is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = str(value)
    return merged


def _event_usage(event):
    """Extract per-call token usage from a backend event when present."""
    if not isinstance(event, dict):
        return {}
    event_type = event.get("type")
    payload = None
    if event_type == "event_msg":
        payload = event.get("payload") or {}
        if payload.get("type") != "token_count":
            return {}
        info = payload.get("info") or {}
        usage = info.get("last_token_usage")
        if isinstance(usage, dict):
            return _normalize_usage(usage)
        usage = info.get("total_token_usage")
        if isinstance(usage, dict):
            return _normalize_usage(usage)
        return {}
    if event_type == "token_count":
        payload = event.get("info") or event.get("payload") or {}
        usage = payload.get("last_token_usage")
        if isinstance(usage, dict):
            return _normalize_usage(usage)
        usage = payload.get("total_token_usage")
        if isinstance(usage, dict):
            return _normalize_usage(usage)
    return {}


def _normalize_usage(usage):
    """Normalize token usage dicts to input/output/total ints."""
    if not isinstance(usage, dict):
        return {}
    input_tokens = _usage_int(
        usage.get("input_tokens"),
        usage.get("prompt_tokens"),
        usage.get("input"),
    )
    output_tokens = _usage_int(
        usage.get("output_tokens"),
        usage.get("completion_tokens"),
        usage.get("output"),
    )
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


def _usage_int(*values):
    """Return the first integer-like usage value from the given candidates."""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return None
