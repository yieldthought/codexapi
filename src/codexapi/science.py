"""Science-mode Ralph loop with logbook output and metric notifications."""

import json
import os
import sys
from datetime import datetime, timezone

from .agent import agent
from .pushover import Pushover
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
    "If this task has some natural figure of merit that would demonstrate any "
    "improvements we made, mention each improvement you have made to it and what "
    "the new best figures are (with absolute values and each one's percentage improvement "
    "over the baseline) when you are finished and report back to me. "
    "Try your best and have fun with this one! If you "
    "think of several options, pick one and run with it - I will not be available "
    "to make decisions for you, I give you my full permission to explore and make "
    "your own best judgement towards our goal! Remember to update SCIENCE.md. "
    "Good hunting!"
)
_LOGBOOK_NAME = "LOGBOOK.md"
_TITLE_PROMPT = (
    "You are naming a run. Return a short descriptive title (max 6 words) that "
    "is likely to be unique for this task. Return only the title text, no quotes, "
    "no punctuation, no markdown. Do not run commands or modify files."
)
_METRICS_PROMPT = (
    "You are a metrics extraction agent. Do NOT attempt the task, do not run "
    "commands, and do not propose next steps. Your job is to read the task and "
    "agent output and extract improved figures of merit.\n"
    "\n"
    "Set new_improvement to true when any figure of merit improved and no other "
    "important metrics meaningfully regress. Use your judgement for "
    "'meaningfully'. If there are no clear metrics or no improvements, set "
    "new_improvement to false.\n"
    "For each metric listed, look to see if there is also a percentage improvement "
    "associated with it - if so, include that under improvement_pct in your output. "
    "Always include the absolute value under value."
    "\n"
    "Return ONLY JSON with keys:\n"
    "  new_improvement: boolean\n"
    "  summary: string (single sentence)\n"
    "  metrics: list of objects with keys:\n"
    "    name: string\n"
    "    value: string (absolute value)\n"
    "    improvement_pct: number or null (percent vs baseline)\n"
    "\n"
)


def _science_parts(task):
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Science task must be a non-empty string.")
    task = task.strip()
    return _SCIENCE_TEMPLATE_A.replace("{task}", task), _SCIENCE_TEMPLATE_B


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
        self._task = task.strip() if isinstance(task, str) else task
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
        self._best_metrics = None
        self._run_title = None
        self._pushover = Pushover()

    def hook_before_loop(self):
        super().hook_before_loop()
        self._pushover.ensure_ready()
        self._run_title = self._build_run_title()

    def build_prompt(self, iteration):
        if iteration <= 1:
            return f"{self._prompt_a}{self._prompt_b}"
        note = _iteration_note(iteration)
        return f"{self._prompt_a}\n\n{note}\n\n{self._prompt_b}"

    def hook_after_iteration(self, iteration, message):
        super().hook_after_iteration(iteration, message)
        self._append_logbook(iteration, message)
        self._extract_and_notify(message)

    def hook_new_best(self, result):
        super().hook_new_best(result)
        summary = _single_line(result.get("summary", "")).strip()
        metrics = result.get("metrics") or []
        message = _format_notification_message(summary, metrics)
        if not message:
            message = "New best metrics detected."
        print(message)
        self._pushover.send(self._run_title, message)

    def _append_logbook(self, iteration, message):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"## Iteration {iteration} - {timestamp}"
        body = (message or "").rstrip()
        entry = "\n".join([header, "", body, "", ""])
        with open(self._logbook_path, "a", encoding="utf-8") as handle:
            handle.write(entry)

    def _extract_and_notify(self, message):
        prompt = _build_metrics_prompt(self._task, message, self._best_metrics)
        try:
            output = agent(prompt, self.cwd, self.yolo, self.flags)
        except Exception as exc:
            _warn(f"Metrics extraction failed: {exc}")
            return
        try:
            result = _parse_metrics(output)
        except ValueError as exc:
            _warn(f"Metrics extraction returned invalid JSON: {exc}")
            return
        if result.get("new_improvement"):
            self._best_metrics = result
            self.hook_new_best(result)

    def _build_run_title(self):
        prompt = "\n".join(
            [
                _TITLE_PROMPT,
                "",
                "TASK:",
                str(self._task or "").strip(),
            ]
        )
        try:
            title = agent(prompt, self.cwd, self.yolo, self.flags)
        except Exception:
            title = ""
        title = _single_line(title).strip()
        if not title:
            title = _fallback_title(self._task)
        return title



def _build_metrics_prompt(task, message, previous_best):
    best_text = "None"
    if previous_best is not None:
        best_text = json.dumps(previous_best, indent=2, sort_keys=True)
    return "\n".join(
        [
            _METRICS_PROMPT,
            "",
            "TASK (context only, do not attempt it):",
            str(task or "").strip(),
            "",
            "PREVIOUS BEST METRICS:",
            best_text,
            "",
            "AGENT OUTPUT:",
            str(message or "").strip(),
        ]
    ).strip()


def _parse_metrics(output):
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON must be an object")
    new_improvement = data.get("new_improvement")
    summary = data.get("summary")
    metrics = data.get("metrics")
    if not isinstance(new_improvement, bool):
        raise ValueError("missing boolean 'new_improvement'")
    if not isinstance(summary, str):
        raise ValueError("missing string 'summary'")
    if not isinstance(metrics, list):
        raise ValueError("metrics must be a list")
    cleaned_metrics = []
    for item in metrics:
        if not isinstance(item, dict):
            raise ValueError("metrics entries must be objects")
        name = item.get("name")
        value = item.get("value")
        improvement_pct = item.get("improvement_pct")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("metric name must be a non-empty string")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("metric value must be a non-empty string")
        if improvement_pct is not None and not isinstance(
            improvement_pct,
            (int, float),
        ):
            raise ValueError("metric improvement_pct must be a number or null")
        cleaned_metrics.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "improvement_pct": improvement_pct,
            }
        )
    return {
        "new_improvement": new_improvement,
        "summary": _single_line(summary),
        "metrics": cleaned_metrics,
    }




def _format_notification_message(summary, metrics):
    parts = []
    metrics_text = _format_metrics(metrics)
    if metrics_text:
        parts.append(f"New best: {metrics_text}")
    if summary:
        parts.append(summary)
    return "\n".join(parts).strip()


def _format_metrics(metrics):
    if not metrics:
        return ""
    rendered = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        name = _single_line(item.get("name", "")).strip()
        value = _single_line(item.get("value", "")).strip()
        improvement = item.get("improvement_pct")
        if not name or not value:
            continue
        if isinstance(improvement, (int, float)):
            rendered.append(f"{name}={value} ({improvement:+.2f}%)")
        else:
            rendered.append(f"{name}={value}")
    return "; ".join(rendered)


def _single_line(text):
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").split())


def _fallback_title(task):
    text = _single_line(task or "").strip()
    if not text:
        return "Science run"
    return text[:77] + "..." if len(text) > 80 else text


def _warn(message):
    print(message, file=sys.stderr)
