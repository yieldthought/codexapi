"""Task wrapper for running Codex Agent flows with checkers."""

import logging

from .agent import Agent

_logger = logging.getLogger(__name__)


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
        yolo=False,
        thread_id=None,
        flags=None,
        full_auto=True,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.prompt = prompt
        self.max_attempts = max_attempts
        self.cwd = cwd
        self.agent = Agent(
            cwd=cwd,
            yolo=yolo,
            thread_id=thread_id,
            flags=flags,
            full_auto=full_auto,
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
