"""Ralph Wiggum-style loop for Codex runs."""

import os
import re
import sys
from datetime import datetime, timezone

from .agent import Agent

_STATE_DIR = ".codexapi"
_STATE_FILE = "ralph-loop.local.md"
_PROMISE_RE = re.compile(r"<promise>(.*?)</promise>", re.DOTALL)


def run_ralph_loop(
    prompt,
    cwd=None,
    yolo=True,
    flags=None,
    max_iterations=0,
    completion_promise=None,
    fresh=True,
):
    """Run a Ralph Wiggum-style loop that repeats the same prompt.

    The loop writes `.codexapi/ralph-loop.local.md` in the target cwd and keeps
    sending the exact same prompt each iteration until one of these happens:
    - A completion promise is matched.
    - `max_iterations` is reached (0 means unlimited).
    - The state file is removed (cancel).
    - An error or KeyboardInterrupt.

    To complete with a promise, the agent must output:
        <promise>TEXT</promise>
    `TEXT` is trimmed and whitespace-collapsed before an exact match against
    `completion_promise`. CRITICAL RULE: If a completion promise is set, you
    may ONLY output it when the statement is completely and unequivocally TRUE.
    Do not output false promises to escape the loop.

    By default each iteration uses a fresh Agent for a clean context. Set
    `fresh=False` to reuse a single Agent instance for shared context.
    Cancel by deleting the state file or running `codexapi ralph --cancel`.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if completion_promise is not None and not isinstance(completion_promise, str):
        raise TypeError("completion_promise must be a string or None")
    if max_iterations < 0:
        raise ValueError("max_iterations must be >= 0")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    state_path = _state_path(cwd)
    _ensure_state_dir(state_path)

    started_at = _utc_now()
    iteration = 1
    _write_state(
        state_path,
        iteration,
        max_iterations,
        completion_promise,
        started_at,
        prompt,
    )

    max_label = str(max_iterations) if max_iterations > 0 else "unlimited"
    if completion_promise is None:
        promise_label = "none (runs forever)"
    else:
        promise_label = (
            f"{completion_promise} (ONLY output when TRUE - do not lie!)"
        )

    print(
        "\n".join(
            [
                "Ralph loop activated.",
                "",
                f"Iteration: {iteration}",
                f"Max iterations: {max_label}",
                f"Completion promise: {promise_label}",
                "",
                "The loop will resend the SAME PROMPT each iteration.",
                "Cancel by deleting .codexapi/ralph-loop.local.md or running",
                "codexapi ralph --cancel.",
                "No manual stop beyond max iterations or completion promise.",
                "",
                "To monitor: head -10 .codexapi/ralph-loop.local.md",
                "",
            ]
        )
    )
    print(prompt)

    if completion_promise is not None:
        print(
            "\n".join(
                [
                    "",
                    "CRITICAL - Ralph Loop Completion Promise",
                    "",
                    "To complete this loop, output this EXACT text:",
                    f"  <promise>{completion_promise}</promise>",
                    "",
                    "STRICT REQUIREMENTS (DO NOT VIOLATE):",
                    "  - Use <promise> XML tags EXACTLY as shown above",
                    "  - The statement MUST be completely and unequivocally TRUE",
                    "  - Do NOT output false statements to exit the loop",
                    "  - Do NOT lie even if you think you should exit",
                    "",
                    "CRITICAL RULE: If a completion promise is set, you may ONLY",
                    "output it when the statement is completely and unequivocally",
                    "TRUE. Do not output false promises to escape the loop, even if",
                    "you think you're stuck or should exit for other reasons. The",
                    "loop is designed to continue until genuine completion.",
                    "",
                ]
            )
        )

    runner = None
    last_message = None
    state_missing = False

    try:
        while True:
            if not os.path.exists(state_path):
                state_missing = True
                print("Ralph loop canceled: state file removed.")
                return last_message

            print(_status_line(iteration, completion_promise))

            if fresh:
                runner = Agent(cwd, yolo, None, flags)
            elif runner is None:
                runner = Agent(cwd, yolo, None, flags)

            message = runner(prompt + '\nIf there are multiple paths forward, you MUST use your own best judgement as to which to try first! Do not ask the user to choose an option, they hereby give you explciit permission to pick the best one yourself.\n')
            print(message)
            last_message = message

            if not os.path.exists(state_path):
                state_missing = True
                print("Ralph loop canceled: state file removed.")
                return last_message

            if max_iterations > 0 and iteration >= max_iterations:
                print(f"Ralph loop: Max iterations ({max_iterations}) reached.")
                return message

            if promise_matches(message, completion_promise):
                print(
                    "Ralph loop: Detected "
                    f"<promise>{completion_promise}</promise>"
                )
                return message

            if not os.path.exists(state_path):
                state_missing = True
                print("Ralph loop canceled: state file removed.")
                return last_message

            iteration += 1
            _write_state(
                state_path,
                iteration,
                max_iterations,
                completion_promise,
                started_at,
                prompt,
            )
    except KeyboardInterrupt:
        print("Ralph loop interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Ralph loop stopped: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if not state_missing:
            _cleanup_state(state_path)


def cancel_ralph_loop(cwd=None):
    """Cancel the Ralph loop by removing the state file."""
    state_path = _state_path(cwd)
    if not os.path.exists(state_path):
        return "No active Ralph loop state found."

    iteration = None
    try:
        fields = _read_state_fields(state_path)
        value = fields.get("iteration")
        if value and value.isdigit():
            iteration = int(value)
    except OSError:
        iteration = None

    _cleanup_state(state_path)

    if iteration is None:
        return "Canceled Ralph loop."
    return f"Canceled Ralph loop at iteration {iteration}."


def promise_matches(message, completion_promise):
    """Return True when the message contains the matching completion promise."""
    if completion_promise is None:
        return False
    extracted = _extract_promise(message)
    if extracted is None:
        return False
    return extracted == completion_promise


def _extract_promise(message):
    """Extract and normalize the first <promise>...</promise> tag from text."""
    match = _PROMISE_RE.search(message)
    if not match:
        return None
    return _normalize_whitespace(match.group(1))


def _normalize_whitespace(text):
    """Trim and collapse whitespace to match the Claude plugin behavior."""
    return " ".join(text.split())


def _state_path(cwd):
    """Return the absolute path for the Ralph loop state file."""
    root = os.fspath(cwd) if cwd else os.getcwd()
    return os.path.join(root, _STATE_DIR, _STATE_FILE)


def _ensure_state_dir(state_path):
    """Ensure the Ralph loop state directory exists."""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)


def _write_state(
    state_path,
    iteration,
    max_iterations,
    completion_promise,
    started_at,
    prompt,
):
    """Write the Ralph loop state file atomically."""
    content = _state_content(
        iteration,
        max_iterations,
        completion_promise,
        started_at,
        prompt,
    )
    temp_path = f"{state_path}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temp_path, state_path)


def _state_content(
    iteration,
    max_iterations,
    completion_promise,
    started_at,
    prompt,
):
    """Build the YAML frontmatter state file content."""
    completion_value = _format_completion_promise(completion_promise)
    lines = [
        "---",
        "active: true",
        f"iteration: {iteration}",
        f"max_iterations: {max_iterations}",
        f"completion_promise: {completion_value}",
        f"started_at: \"{started_at}\"",
        "---",
        "",
        prompt,
    ]
    return "\n".join(lines)


def _format_completion_promise(completion_promise):
    """Format the completion promise to match the plugin frontmatter."""
    if completion_promise is None:
        return "null"
    return f"\"{completion_promise}\""


def _read_state_fields(state_path):
    """Read YAML frontmatter fields from the Ralph loop state file."""
    with open(state_path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _cleanup_state(state_path):
    """Remove the Ralph loop state file if it exists."""
    try:
        os.remove(state_path)
    except FileNotFoundError:
        return


def _status_line(iteration, completion_promise):
    """Build the per-iteration status line for the Ralph loop."""
    if completion_promise is None:
        return (
            f"Ralph iteration {iteration} | "
            "No completion promise set - loop runs infinitely"
        )
    return (
        f"Ralph iteration {iteration} | To stop: output "
        f"<promise>{completion_promise}</promise> "
        "(ONLY when statement is TRUE - do not lie to exit!)"
    )


def _utc_now():
    """Return a UTC timestamp string matching the Claude plugin."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
