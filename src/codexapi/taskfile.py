"""Load YAML task files and map them onto Task hooks."""

import yaml

from .task import AutoTask

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

    max_iterations = data.get("max_iterations")
    if max_iterations is not None:
        if not isinstance(max_iterations, int):
            raise ValueError("Task file max_iterations must be an integer.")
        if max_iterations < 0:
            raise ValueError("Task file max_iterations must be >= 0.")

    return {
        "prompt": prompt,
        "set_up": _optional_str(data.get("set_up")),
        "tear_down": _optional_str(data.get("tear_down")),
        "check": _optional_str(data.get("check")),
        "on_success": _optional_str(data.get("on_success")),
        "on_failure": _optional_str(data.get("on_failure")),
        "max_iterations": max_iterations,
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


def task_def_uses_item(task_def):
    """Return True if a task definition includes the {{item}} placeholder."""
    if not isinstance(task_def, dict):
        raise TypeError("task definition must be a dict")
    for key in ("prompt", "set_up", "tear_down", "check", "on_success", "on_failure"):
        value = task_def.get(key)
        if isinstance(value, str) and _ITEM_TOKEN in value:
            return True
    return False


class TaskFile(AutoTask):
    """Task subclass that maps a YAML task file onto Task hooks."""

    def __init__(
        self,
        path,
        item=None,
        max_iterations=None,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        task_def = load_task_file(path)
        if max_iterations is None:
            max_iterations = task_def.get("max_iterations")
        elif not isinstance(max_iterations, int):
            raise ValueError("max_iterations must be an integer.")
        elif max_iterations < 0:
            raise ValueError("max_iterations must be >= 0.")
        item_text = "" if item is None else str(item)
        rendered = {
            "prompt": _render(task_def.get("prompt"), item_text),
            "set_up": _render(task_def.get("set_up"), item_text),
            "tear_down": _render(task_def.get("tear_down"), item_text),
            "check": _render(task_def.get("check"), item_text),
            "on_success": _render(task_def.get("on_success"), item_text),
            "on_failure": _render(task_def.get("on_failure"), item_text),
        }
        if max_iterations is None:
            super().__init__(
                rendered["prompt"],
                rendered["check"],
                cwd=cwd,
                yolo=yolo,
                thread_id=thread_id,
                flags=flags,
                set_up=rendered["set_up"],
                tear_down=rendered["tear_down"],
                on_success=rendered["on_success"],
                on_failure=rendered["on_failure"],
            )
            return
        super().__init__(
            rendered["prompt"],
            rendered["check"],
            max_iterations,
            cwd,
            yolo,
            thread_id,
            flags,
            set_up=rendered["set_up"],
            tear_down=rendered["tear_down"],
            on_success=rendered["on_success"],
            on_failure=rendered["on_failure"],
        )
