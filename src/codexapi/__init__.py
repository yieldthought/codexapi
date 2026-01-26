"""Minimal Python API for running the Codex CLI."""

from .agent import Agent, agent
from .foreach import ForeachResult, foreach
from .task import Task, TaskFailed, TaskResult, task, task_result

__all__ = [
    "Agent",
    "ForeachResult",
    "Task",
    "TaskFailed",
    "TaskResult",
    "agent",
    "foreach",
    "task",
    "task_result",
]
__version__ = "0.5.17"
