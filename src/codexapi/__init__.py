"""Minimal Python API for running the Codex CLI."""

from .agent import Agent, agent
from .task import Task, TaskFailed, TaskResult, task, task_result

__all__ = [
    "Agent",
    "Task",
    "TaskFailed",
    "TaskResult",
    "agent",
    "task",
    "task_result",
]
__version__ = "0.4.0"
