"""Minimal Python API for running the Codex CLI."""

from .agent import Agent, agent
from .task import Task, TaskResult

__all__ = ["Agent", "Task", "TaskResult", "agent"]
__version__ = "0.1.2"
