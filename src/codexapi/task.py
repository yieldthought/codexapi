"""Task wrapper for running Codex Agent flows with checkers."""

import json
import logging
import time

from .agent import Agent, agent

_logger = logging.getLogger(__name__)

_CHECK_PREFIX = (
    "You are a verification agent. Explore this workspace and carefully evaluate it "
    "against the check below. Collect evidence by running any tests and/or reading "
    "and tracing through code, but do not change any of the code.\n"
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


def _default_check(prompt):
    return (
        "Verify that the task below has been completed in line with the original intent.\n"
        "Task:\n"
        "```\n"
        f"{prompt}\n"
        "```"
    )


def _build_check_prompt(check):
    return f"{_CHECK_PREFIX}\n\n{check}\n\n{_CHECK_SUFFIX}"


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


def _print_progress(
    attempt,
    total,
    start_time,
    agent_output,
    check_output,
    cwd,
    yolo,
    flags,
):
    elapsed = time.monotonic() - start_time
    remaining = 0
    if attempt:
        remaining = (elapsed / attempt) * (total - attempt)

    summary_prompt = _build_progress_prompt(agent_output, check_output)
    summary = agent(summary_prompt, cwd, yolo, flags)
    agent_summary, check_summary = _progress_result(summary)

    elapsed_text = _format_duration(elapsed)
    remaining_text = _format_duration(remaining)
    print(
        f"Round {attempt}/{total} ({elapsed_text} elapsed, {remaining_text} remaining)",
        flush=True,
    )
    print(f"Agent: {agent_summary}", flush=True)
    print(f"Check: {check_summary}", flush=True)
    print("", flush=True)

def _fix_prompt(error):
    return (
        "The verification check failed:\n"
        f"{error}\n\n"
        "Please fix the issues while staying close to the original intent."
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


def task(
    prompt,
    check=None,
    n=10,
    cwd=None,
    yolo=True,
    flags=None,
    progress=False,
):
    """Run a prompt with optional checker-driven retries.

    Args:
        prompt: The task prompt to run.
        check: False to skip verification, None for the default check, or
            a string check prompt.
        n: Maximum number of retries after a failed check.
        cwd: Optional working directory for the Codex session.
        yolo: Whether to pass --yolo to Codex.
        flags: Additional raw CLI flags to pass to Codex.
        progress: Whether to print progress after each verification round.

    Returns:
        The agent's response text when the task succeeds.

    Raises:
        TaskFailed: when the task reaches the maximum attempts without success.
    """
    result = task_result(prompt, check, n, cwd, yolo, flags, progress)
    if result.success:
        return result.summary
    raise TaskFailed(result.summary, result.attempts, result.errors)


def task_result(
    prompt,
    check=None,
    n=10,
    cwd=None,
    yolo=True,
    flags=None,
    progress=False,
):
    """Run a prompt with optional checker-driven retries and return TaskResult.

    The runner keeps a single session. Each verification attempt uses a fresh,
    stateless agent call. When progress is True, print a summary each round.
    """
    if check is False:
        runner = Agent(cwd, yolo, None, flags)
        start_time = time.monotonic()
        summary = runner(prompt)
        if progress:
            _print_progress(
                1,
                1,
                start_time,
                summary,
                "Verification skipped.",
                cwd,
                yolo,
                flags,
            )
        return TaskResult(True, summary, 1, None, runner.thread_id)
    if check is None:
        check = _default_check(prompt)
    if not isinstance(check, str):
        raise TypeError("check must be a string or False")
    if n < 0:
        raise ValueError("n must be >= 0")

    runner = Agent(cwd, yolo, None, flags)
    start_time = time.monotonic()
    last_output = runner(prompt)
    check_prompt = _build_check_prompt(check)

    for attempt in range(n + 1):
        check_output = agent(check_prompt, cwd, yolo, flags)
        success, reason = _check_result(check_output)
        if progress:
            _print_progress(
                attempt + 1,
                n + 1,
                start_time,
                last_output,
                check_output,
                cwd,
                yolo,
                flags,
            )
        if success:
            summary = runner(_success_prompt())
            return TaskResult(
                True,
                summary,
                attempt + 1,
                None,
                runner.thread_id,
            )
        if attempt == n:
            summary = runner(_failure_prompt(reason))
            return TaskResult(
                False,
                summary,
                attempt + 1,
                reason,
                runner.thread_id,
            )
        last_output = runner(_fix_prompt(reason))


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
        max_attempts=10,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.prompt = prompt
        self.max_attempts = max_attempts
        self.cwd = cwd
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

    def check(self):
        """ Check if the task is done, return a string describing the problems if not.
            This can be any combination of running tests, python code or running an agent
            with a specific prompt in self.cwd.
         """

    def on_success(self, result):
        """Hook called after a successful task, e.g. commit the changes."""

    def on_failure(self, result):
        """Hook called after a failed run, e.g. log the failure reason."""

    def fix_prompt(self, error):
        """Build a prompt that asks the agent to fix checker failures."""
        return (
            "The following checks failed:\n"
            f"{error}\n\n"
            "Can you please dive in and see if you agree with this assessment, then fix these issues while staying as close as you can to the spirit of the original task?"
        )

    def success_prompt(self):
        """Ask the agent to summarize what it did."""
        return "Awesome - great job! Can you please produce a short summary of what you've done?"

    def failure_prompt(self, error):
        """Ask the agent to summarize remaining issues after retries."""
        return (
            "We ran out of attempts. Can you please look back at everything you tried and summarize what it was that made this task too hard to complete, including anything you wish you'd known at the start that would have helped improve things?\n\n"
            f"Outstanding issues:\n{error}"
        )

    def __call__(self, debug=False):
        """Run the task with checker-driven retries.
            If debug is True, log debug messages.
        """
        try:
            # If this fails in the middle we will still try to tear down
            self.set_up()

            # Start with the initial prompt
            output = self.agent(self.prompt)
            if debug:
                _logger.debug("Initial output: %s", output)
            
            # Try correcting it up to max_attempts times
            for attempt in range(self.max_attempts):
                error = self.check()
                if debug:
                    _logger.debug("Check error: %s", error)
                    
                if error:
                    # if there were errors, tell the agent to fix them
                    output = self.agent(self.fix_prompt(error))
                    if debug:
                        _logger.debug("Fix output: %s", output)
                else:
                    # otherwise get a summary of what was done and run on_success
                    summary = self.agent(self.success_prompt())
                    if debug:
                        _logger.debug("Success summary: %s", summary)
                    result = TaskResult(True, summary, attempt + 1, error, self.agent.thread_id)
                    self.on_success(result)
                    return result

            # Ran out of attempts - get a reason why and run on_failure
            summary = self.agent(self.failure_prompt(error))
            if debug:
                _logger.debug("Failure summary: %s", summary)
            result = TaskResult(False, summary, attempt + 1, error, self.agent.thread_id)
            self.on_failure(result)
            return result
        finally:
            # No matter what, once we have set_up we will always tear_down
            self.tear_down()
