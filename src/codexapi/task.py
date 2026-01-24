"""Task wrapper for running Codex Agent flows with checkers."""

import json
import logging
import time

from .agent import Agent, agent

_logger = logging.getLogger(__name__)

_CHECK_PREFIX = (
    "You are a verification agent. Explore this workspace and carefully evaluate it "
    "against the instructions below. Collect evidence by running any tests and/or "
    "reading and tracing through code, but do not change any of the code.\n"
    "You will receive the task or check instructions first, then the agent output "
    "under the heading 'AGENT OUTPUT', which is provided for context and does not "
    "replace or supersede collecting your own evidence unless it is clear from the "
    "instructions that the agent's output IS the expected output of the task.\n"
    "Return only JSON with keys: success (boolean) and reason (string).\n"
    "Set success to true only if everything matches the intent."
)
_CHECK_SUFFIX = "JSON only. No markdown or extra text."
_PROGRESS_PROMPT = (
    "Summarize the outputs below in one line each.\n"
    "Return only JSON with keys: agent (string) and check (string).\n"
    "Each value must be a single line with no newlines.\n"
    "Do not run commands or change any files."
)
DEFAULT_MAX_ITERATIONS = 10


def _default_check(prompt):
    return (
        "Verify that the task below has been completed in line with the original intent.\n"
        "Task:\n"
        "```\n"
        f"{prompt}\n"
        "```"
    )


def _build_check_prompt(check, agent_output):
    output = agent_output or ""
    return (
        f"{_CHECK_PREFIX}\n\n"
        f"{check}\n\n"
        "AGENT OUTPUT:\n"
        f"{output}\n\n"
        f"{_CHECK_SUFFIX}"
    )


def _resolve_check_text(prompt, check):
    if check is False:
        return None, True
    if check is None:
        return _default_check(prompt), False
    if not isinstance(check, str):
        raise TypeError("check must be a string or False")
    if check.strip() == "None":
        return None, True
    return check, False


def _build_progress_prompt(agent_output, check_output):
    return (
        f"{_PROGRESS_PROMPT}\n\n"
        "AGENT OUTPUT:\n"
        f"{agent_output}\n\n"
        "CHECK OUTPUT:\n"
        f"{check_output}"
    )


def _check_result(output):
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        return False, f"Checker returned invalid JSON: {exc}"

    if not isinstance(data, dict):
        return False, "Checker JSON must be an object."

    success = data.get("success")
    reason = data.get("reason")
    if not isinstance(success, bool):
        return False, "Checker JSON missing boolean 'success'."
    if not isinstance(reason, str):
        return False, "Checker JSON missing string 'reason'."

    return success, reason.strip()


def _progress_result(output):
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Progress summary returned invalid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Progress summary JSON must be an object.")

    agent_summary = data.get("agent")
    check_summary = data.get("check")
    if not isinstance(agent_summary, str):
        raise RuntimeError("Progress summary JSON missing string 'agent'.")
    if not isinstance(check_summary, str):
        raise RuntimeError("Progress summary JSON missing string 'check'.")

    return _single_line(agent_summary), _single_line(check_summary)


def _single_line(text):
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").split())


def _format_duration(seconds):
    if seconds < 0:
        seconds = 0
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    if not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _progress_round_label(attempt, total):
    if not total:
        return f"Round {attempt}/unlimited"
    return f"Round {attempt}/{total}"


def _print_progress_start(attempt, total):
    print(_progress_round_label(attempt, total), flush=True)


def _print_progress_result(
    attempt,
    total,
    start_time,
    agent_output,
    check_output,
    cwd,
    yolo,
    flags,
    success,
):
    elapsed = time.monotonic() - start_time
    remaining = 0
    remaining_text = "unknown"
    if total and attempt:
        remaining = (elapsed / attempt) * (total - attempt)
        remaining_text = _format_duration(remaining)

    summary_prompt = _build_progress_prompt(agent_output, check_output)
    summary = agent(summary_prompt, cwd, yolo, flags)
    agent_summary, check_summary = _progress_result(summary)

    elapsed_text = _format_duration(elapsed)
    print(f"Agent: {agent_summary}", flush=True)
    print(f"Check: {check_summary}", flush=True)
    verdict = "success" if success else "failure"
    print(
        f"Verdict: {verdict} ({elapsed_text} elapsed, {remaining_text} remaining)",
        flush=True,
    )
    print("", flush=True)

def _fix_prompt(error):
    return (
        "Thanks for your work. An automated verifier reported these issues:\n"
        f"{error}\n\n"
        "Take another look and see whether you agree and, if so, please take this "
        "feedback into consideration and use it to continue to make progress "
        "towards our original goal and intent."
    )


def _success_prompt():
    return "Verified. Please summarize what you did."


def _failure_prompt(error):
    return (
        "We ran out of attempts. Summarize what you did and what is still failing.\n\n"
        f"Outstanding issues:\n{error}"
    )


class TaskFailed(RuntimeError):
    """Raised when a task hits the maximum attempts without success."""

    def __init__(self, summary, attempts=None, errors=None):
        message = "Task failed after maximum attempts."
        if summary:
            message = f"{message}\n{summary}"
        super().__init__(message)
        self.summary = summary
        self.attempts = attempts
        self.errors = errors


def _validate_hook(name, value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TypeError(f"{name} must be a string or None")


def task(
    prompt,
    check=None,
    max_iterations=DEFAULT_MAX_ITERATIONS,
    cwd=None,
    yolo=True,
    flags=None,
    progress=False,
    set_up=None,
    tear_down=None,
    on_success=None,
    on_failure=None,
):
    """Run a prompt with optional checker-driven retries.

    Args:
        prompt: The task prompt to run.
        check: False to skip verification, None for the default check, or
            a string check prompt. The string "None" skips verification.
        max_iterations: Maximum number of task attempts (0 means unlimited).
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        flags: Additional raw CLI flags to pass to Codex.
        progress: Whether to print progress after each verification round.
        set_up: Optional setup prompt to run before the task.
        tear_down: Optional cleanup prompt to run after the task.
        on_success: Optional prompt to run after a successful task.
        on_failure: Optional prompt to run after a failed task.

    Returns:
        The agent's response text when the task succeeds.

    Raises:
        TaskFailed: when the task reaches the maximum attempts without success.
    """
    result = task_result(
        prompt,
        check,
        max_iterations,
        cwd,
        yolo,
        flags,
        progress,
        set_up,
        tear_down,
        on_success,
        on_failure,
    )
    if result.success:
        return result.summary
    raise TaskFailed(result.summary, result.attempts, result.errors)


def task_result(
    prompt,
    check=None,
    max_iterations=DEFAULT_MAX_ITERATIONS,
    cwd=None,
    yolo=True,
    flags=None,
    progress=False,
    set_up=None,
    tear_down=None,
    on_success=None,
    on_failure=None,
):
    """Run a prompt with optional checker-driven retries and return TaskResult.

    The runner keeps a single session. Each verification attempt uses a fresh,
    stateless agent call. When progress is True, print a summary each round.

    Hook strings mirror task file keys: set_up, tear_down, on_success, on_failure.
    """
    if max_iterations < 0:
        raise ValueError("max_iterations must be >= 0")
    if not (check is None or check is False or isinstance(check, str)):
        raise TypeError("check must be a string or False")

    set_up_text = _validate_hook("set_up", set_up)
    tear_down_text = _validate_hook("tear_down", tear_down)
    on_success_text = _validate_hook("on_success", on_success)
    on_failure_text = _validate_hook("on_failure", on_failure)
    runner = AutoTask(
        prompt,
        check,
        max_iterations,
        cwd,
        yolo,
        None,
        flags,
        set_up=set_up_text,
        tear_down=tear_down_text,
        on_success=on_success_text,
        on_failure=on_failure_text,
    )
    return runner(progress=progress)


class TaskResult:
    """Outcome summary for a task run."""

    def __init__(self, success, summary, attempts, errors, thread_id):
        self.success = success
        self.summary = summary
        self.attempts = attempts
        self.errors = errors
        self.thread_id = thread_id

    def __repr__(self):
        return (
            "TaskResult("
            f"success={self.success}, "
            f"attempts={self.attempts}, "
            f"errors={self.errors!r}, "
            f"thread_id={self.thread_id!r}, "
            f"summary={self.summary!r}"
            ")"
        )


class Task:
    """ Run a Codex Agent in a directory until it is verifiably done.
        Subclass and override these functions:
            set_up     : prepare working directory, install things etc.
            tear_down  : undo the above and leave machine in a clean state
            check      : check if the task is done, return an error string if not
            on_success : run if the task succeeds, e.g. commit and push
            on_failure : run if the tsak fails, e.g. record why

    """

    def __init__(
        self,
        prompt,
        max_attempts=DEFAULT_MAX_ITERATIONS,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        if max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        self.prompt = prompt
        self.max_attempts = max_attempts
        self.cwd = cwd
        self.last_output = None
        self.last_check_output = None
        self.check_skipped = False
        self.check_text = None
        self._yolo = yolo
        self._flags = flags
        self.agent = Agent(
            cwd,
            yolo,
            thread_id,
            flags,
        )

    def set_up(self):
        """Clone a repo, set up a directory etc."""

    def tear_down(self):
        """Delete the directory etc."""

    def check(self, output=None):
        """Check if the task is done, return a string describing problems if not.

        The default implementation runs the verifier agent with the standard
        check wrapper and expects JSON output.
        """
        self.last_check_output = None
        self.check_skipped = False
        check_text, skip = _resolve_check_text(self.prompt, self.check_text)
        if skip:
            self.check_skipped = True
            return None
        last_output = output if output is not None else self.last_output
        last_output = last_output or ""
        check_prompt = _build_check_prompt(check_text, last_output)
        check_output = agent(check_prompt, self.cwd, self._yolo, self._flags)
        self.last_check_output = check_output
        success, reason = _check_result(check_output)
        if success:
            return None
        return reason

    def on_success(self, result):
        """Hook called after a successful task, e.g. commit the changes."""

    def on_failure(self, result):
        """Hook called after a failed run, e.g. log the failure reason."""

    def fix_prompt(self, error):
        """Build a prompt that asks the agent to fix checker failures."""
        return (
            "Thanks for your work. An automated verifier reported these issues:\n"
            f"{error}\n\n"
            "Take another look and see whether you agree and, if so, please take "
            "this feedback into consideration and use it to continue to make "
            "progress towards our original goal and intent. Don't propose next steps, "
            "use your best judgement and work towards the goal!"
        )

    def success_prompt(self):
        """Ask the agent to summarize what it did."""
        return _success_prompt()

    def failure_prompt(self, error):
        """Ask the agent to summarize remaining issues after retries."""
        return _failure_prompt(error)

    def __call__(self, debug=False, progress=False):
        """Run the task with checker-driven retries.
            If debug is True, log debug messages.
            If progress is True, print progress after each verification round.
        """
        try:
            # If this fails in the middle we will still try to tear down
            self.set_up()

            # Start with the initial prompt
            output = self.agent(self.prompt)
            self.last_output = output
            if debug:
                _logger.debug("Initial output: %s", output)

            # Try correcting it up to max_attempts times
            start_time = time.monotonic()
            error = None
            attempt = 0
            while True:
                attempt += 1
                if progress:
                    _print_progress_start(
                        attempt,
                        self.max_attempts,
                    )
                error = self.check(self.last_output)
                if debug:
                    _logger.debug("Check error: %s", error)

                if progress:
                    check_output = self.last_check_output
                    if self.check_skipped:
                        check_output = "Verification skipped."
                    _print_progress_result(
                        attempt,
                        self.max_attempts,
                        start_time,
                        self.last_output,
                        check_output or "",
                        self.cwd,
                        self._yolo,
                        self._flags,
                        not error,
                    )
                if not error:
                    summary = self.agent(self.success_prompt())
                    if debug:
                        _logger.debug("Success summary: %s", summary)
                    result = TaskResult(
                        True,
                        summary,
                        attempt,
                        None,
                        self.agent.thread_id,
                    )
                    self.on_success(result)
                    return result
                if self.max_attempts and attempt >= self.max_attempts:
                    summary = self.agent(self.failure_prompt(error))
                    if debug:
                        _logger.debug("Failure summary: %s", summary)
                    result = TaskResult(
                        False,
                        summary,
                        attempt,
                        error,
                        self.agent.thread_id,
                    )
                    self.on_failure(result)
                    return result
                output = self.agent(self.fix_prompt(error))
                self.last_output = output
                if debug:
                    _logger.debug("Fix output: %s", output)
        finally:
            # No matter what, once we have set_up we will always tear_down
            self.tear_down()


class AutoTask(Task):
    """Task subclass that maps prompt strings onto Task hooks."""

    def __init__(
        self,
        prompt,
        check=None,
        max_attempts=DEFAULT_MAX_ITERATIONS,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
        set_up=None,
        tear_down=None,
        on_success=None,
        on_failure=None,
    ):
        if not (check is None or check is False or isinstance(check, str)):
            raise TypeError("check must be a string or False")
        if max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        super().__init__(prompt, max_attempts, cwd, yolo, thread_id, flags)
        self.check_text = check
        self._set_up = _validate_hook("set_up", set_up)
        self._tear_down = _validate_hook("tear_down", tear_down)
        self._on_success = _validate_hook("on_success", on_success)
        self._on_failure = _validate_hook("on_failure", on_failure)

    def _run_hook(self, text):
        if text:
            agent(text, self.cwd, self._yolo, self._flags)

    def set_up(self):
        self._run_hook(self._set_up)

    def tear_down(self):
        self._run_hook(self._tear_down)

    def on_success(self, result):
        self._run_hook(self._on_success)

    def on_failure(self, result):
        self._run_hook(self._on_failure)
