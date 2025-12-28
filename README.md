# CodexAPI

Use codex from python as easily as calling a function with your codex credits instead of the API.

## Requirements

- Codex CLI installed and authenticated (`codex` must be on your PATH).
- Python 3.8+.

## Install

```bash
pip install codexapi
```

## Quickstart

```python
from codexapi import agent, Agent, Task

# Run one-shot tasks as a function call
print(agent("Say hello"))

# Run a multi-turn conversation as a session
session = Agent(cwd="/path/to/project")
print(session("Summarize this repo."))
print(session("Now list any risks."))

# Save and resume a session later
thread_id = session.thread_id
session2 = Agent(cwd="/path/to/project", thread_id=thread_id)
print(session2("Continue from where we left off."))

# Define a task with a checker
class RepoTask(Task):
    def check(self):
        # Return an error string if something is wrong, or None/"" if OK
        return None

task = RepoTask("Summarize this repo.", cwd="/path/to/project")
result = task()
print(result.success, result.summary)
```

## API

### `agent(prompt, cwd=None, yolo=False, flags=None) -> str`

Runs a single Codex turn and returns only the agent's message. Any reasoning
items are filtered out.

- `prompt` (str): prompt to send to Codex.
- `cwd` (str | PathLike | None): working directory for the Codex session.
- `yolo` (bool): pass `--yolo` to Codex when true.
- `flags` (str | None): extra CLI flags to pass to Codex.

### `Agent(cwd=None, yolo=False, thread_id=None, flags=None)`

Creates a stateful session wrapper. Calling the instance sends the prompt into
the same conversation and returns only the agent's message.

- `__call__(prompt) -> str`: send a prompt to Codex and return the message.
- `thread_id -> str | None`: expose the underlying session id once created.
- `yolo` (bool): pass `--yolo` to Codex when true.
- `flags` (str | None): extra CLI flags to pass to Codex.

### `Task(prompt, max_attempts=10, cwd=None, yolo=False, thread_id=None, flags=None)`

Runs a Codex task with checker-driven retries. Subclass it and implement
`check()` to return an error string when the task is incomplete, or return
`None`/`""` when the task passes.

- `__call__() -> TaskResult`: run the task.
- `set_up()`: optional setup hook.
- `tear_down()`: optional cleanup hook.
- `check() -> str | None`: return an error description or `None`/`""`.
- `on_success(result)`: optional success hook.
- `on_failure(result)`: optional failure hook.

### `TaskResult(success, summary, attempts, errors, thread_id)`

Simple result object returned by `Task.__call__`.

- `success` (bool): whether the task completed successfully.
- `summary` (str): agent summary of what happened.
- `attempts` (int): how many attempts were used.
- `errors` (str | None): last checker error, if any.
- `thread_id` (str | None): Codex thread id for the session.

## Behavior notes

- Uses `codex exec --json` and parses JSONL events for `agent_message` items.
- Automatically passes `--skip-git-repo-check` so it can run outside a git repo.
- Passes `--yolo` when enabled (use with care).
- Raises `RuntimeError` if Codex exits non-zero or returns no agent message.

## Configuration

Set `CODEX_BIN` to point at a non-default Codex binary:

```bash
export CODEX_BIN=/path/to/codex
```
