"""Load YAML task files and map them onto Task hooks."""

import yaml

from .agent import agent
from .task import Task

_ITEM_TOKEN = "{{item}}"


def load_task_file(path):
    """Load a YAML task file and return a normalized task definition."""
    if not path:
        raise ValueError("task file path is required")
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("Task file must be a YAML mapping.")

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Task file missing non-empty 'prompt'.")

    return {
        "prompt": prompt,
        "set_up": _optional_str(data.get("set_up")),
        "tear_down": _optional_str(data.get("tear_down")),
        "check": _optional_str(data.get("check")),
        "on_success": _optional_str(data.get("on_success")),
        "on_failure": _optional_str(data.get("on_failure")),
    }


def _optional_str(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    raise ValueError("Task file values must be strings.")


def _render(text, item):
    if text is None:
        return None
    if item is None:
        return text
    return text.replace(_ITEM_TOKEN, item)


class AutoTask(Task):
    """Task subclass that maps YAML strings onto Task hooks."""

    def __init__(
        self,
        config,
        item=None,
        max_attempts=10,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        if not isinstance(config, dict):
            raise TypeError("config must be a task definition dict")
        self._config = config
        self._item = "" if item is None else str(item)
        self._yolo = yolo
        self._flags = flags
        prompt = _render(config.get("prompt"), self._item)
        super().__init__(prompt, max_attempts, cwd, yolo, thread_id, flags)

    def _hook(self, name):
        return _render(self._config.get(name), self._item)

    def set_up(self):
        text = self._hook("set_up")
        if text:
            agent(text, self.cwd, self._yolo, self._flags)

    def tear_down(self):
        text = self._hook("tear_down")
        if text:
            agent(text, self.cwd, self._yolo, self._flags)

    def check(self, output=None):
        text = self._hook("check")
        if not text:
            return None
        last_output = output if output is not None else self.last_output
        last_output = last_output or ""
        if last_output:
            prompt = f"{text}\n\nAGENT OUTPUT:\n{last_output}"
        else:
            prompt = text
        result = agent(prompt, self.cwd, self._yolo, self._flags)
        if not isinstance(result, str) or not result.strip():
            return None
        return result

    def on_success(self, result):
        text = self._hook("on_success")
        if text:
            agent(text, self.cwd, self._yolo, self._flags)

    def on_failure(self, result):
        text = self._hook("on_failure")
        if text:
            agent(text, self.cwd, self._yolo, self._flags)
