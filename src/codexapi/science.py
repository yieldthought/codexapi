"""Science-mode Ralph loop with logbook output."""

import os
from datetime import datetime, timezone

from .ralph import Ralph

_SCIENCE_TEMPLATE_A = (
    "Good afternoon! We have a fun task today - take a good look around this repo "
    "and review all relevant knowledge you have. Our task is to {task}. We're "
    "working step by step in a scientific manner so if there's a SCIENCE.md read "
    "that first to understand the progress of the rest of the team so far. Then "
    "try as hard as you can to find a good path forwards - run as many experiments "
    "as you want and take your time, we have all night. Note down everything you "
    "learn that wasn't obvious in a knowledge section in SCIENCE.md and any "
    "experiments in a similar section. The aim is to move the ball forwards, "
    "either by getting closer to the goal ruling out a hypothesis that doesn't "
    "whilst understanding why. "
)
_SCIENCE_TEMPLATE_B = (
    "Try your best and have fun with this one! If you "
    "think of several options, pick one and run with it - I will not be available "
    "to make decisions for you, I give you my full permission to explore and make "
    "your own best judgement towards our goal! Remember to update SCIENCE.md. "
    "Good hunting!"
)
_LOGBOOK_NAME = "LOGBOOK.md"


def _science_parts(task):
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Science task must be a non-empty string.")
    task = task.strip()
    return _SCIENCE_TEMPLATE_A.replace("{task}", task), _SCIENCE_TEMPLATE_B


def _science_prompt(task):
    part_a, part_b = _science_parts(task)
    return f"{part_a}{part_b}"


def _logbook_path(cwd):
    root = os.fspath(cwd) if cwd else os.getcwd()
    return os.path.join(root, _LOGBOOK_NAME)


def _iteration_note(iteration):
    return (
        f"We are now in iteration {iteration}. Before deciding on your next steps, "
        "review LOGBOOK.md to see what was done and proposed in previous iterations. "
        "Treat all questions in there as suggestions only. You may decide it's time "
        "to try a completely different tack, or you may see something that feels like "
        "a follow-up that should be investigated. I trust your good judgement! Do not "
        "write to LOGBOOK.md, it will be updated automatically when we have finished."
    )


class Science(Ralph):
    """Science-mode Ralph runner that logs each iteration output."""

    def __init__(
        self,
        task,
        cwd=None,
        yolo=True,
        flags=None,
        max_iterations=0,
        completion_promise=None,
        fresh=True,
    ):
        prompt_a, prompt_b = _science_parts(task)
        prompt = f"{prompt_a}{prompt_b}"
        super().__init__(
            prompt,
            cwd,
            yolo,
            flags,
            max_iterations,
            completion_promise,
            fresh,
        )
        self._prompt_a = prompt_a
        self._prompt_b = prompt_b
        self._logbook_path = _logbook_path(cwd)

    def build_prompt(self, iteration):
        if iteration <= 1:
            return f"{self._prompt_a}{self._prompt_b}"
        note = _iteration_note(iteration)
        return f"{self._prompt_a}\n\n{note}\n\n{self._prompt_b}"

    def hook_after_iteration(self, iteration, message):
        super().hook_after_iteration(iteration, message)
        self._append_logbook(iteration, message)

    def _append_logbook(self, iteration, message):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"## Iteration {iteration} - {timestamp}"
        body = (message or "").rstrip()
        entry = "\n".join([header, "", body, "", ""])
        with open(self._logbook_path, "a", encoding="utf-8") as handle:
            handle.write(entry)
