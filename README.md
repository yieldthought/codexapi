# CodexAPI

Use OpenAI's codex from python as easily as calling a function with your codex credits instead of the API.

*Note: this project is not affiliated with OpenAI in any way. Thanks for the awesome tools and models though!*

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

## CLI

After installing, use the `codexapi` command:

```bash
codexapi run "Summarize this repo."
codexapi run --cwd /path/to/project "Fix the failing tests."
echo "Say hello." | codexapi run
```

`codexapi task` exits with code 0 on success and 1 on failure, printing the summary.

```bash
codexapi task "Fix the failing tests." --max-iterations 5
```

Show running sessions and their latest activity:

```bash
codexapi top
```
Press `h` for keys.

Resume a session and print the thread id to stderr:

```bash
codexapi run --thread-id THREAD_ID --print-thread-id "Continue where we left off."
```

Use `--no-yolo` to run Codex with `--full-auto` instead.

Ralph loop mode repeats the same prompt until a completion promise or a max
iteration cap is hit (0 means unlimited). Cancel by deleting
`.codexapi/ralph-loop.local.md` or running `codexapi ralph --cancel`.

```bash
codexapi ralph "Fix the bug." --completion-promise DONE --max-iterations 5
codexapi ralph --ralph-fresh "Try again from scratch." --max-iterations 3
codexapi ralph --cancel --cwd /path/to/project
```

## API

### `agent(prompt, cwd=None, yolo=True, flags=None) -> str`

Runs a single Codex turn and returns only the agent's message. Any reasoning
items are filtered out.

- `prompt` (str): prompt to send to Codex.
- `cwd` (str | PathLike | None): working directory for the Codex session.
- `yolo` (bool): pass `--yolo` to Codex when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to Codex.

### `Agent(cwd=None, yolo=True, thread_id=None, flags=None)`

Creates a stateful session wrapper. Calling the instance sends the prompt into
the same conversation and returns only the agent's message.

- `__call__(prompt) -> str`: send a prompt to Codex and return the message.
- `thread_id -> str | None`: expose the underlying session id once created.
- `yolo` (bool): pass `--yolo` to Codex when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to Codex.

### `task(prompt, check=None, n=10, cwd=None, yolo=True, flags=None) -> str`

Runs a task with checker-driven retries and returns the success summary.
Raises `TaskFailed` when the maximum attempts are reached.

- `check` (str | None | False): custom check prompt, default checker, or `False` to skip.
- `n` (int): maximum number of retries after a failed check.

### `task_result(prompt, check=None, n=10, cwd=None, yolo=True, flags=None) -> TaskResult`

Runs a task with checker-driven retries and returns a `TaskResult` without
raising `TaskFailed`.

### `Task(prompt, max_attempts=10, cwd=None, yolo=True, thread_id=None, flags=None)`

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

### `TaskFailed`

Exception raised by `task()` when retries are exhausted.

- `summary` (str): failure summary text.
- `attempts` (int | None): attempts made when the task failed.
- `errors` (str | None): last checker error, if any.

## Behavior notes

- Uses `codex exec --json` and parses JSONL events for `agent_message` items.
- Automatically passes `--skip-git-repo-check` so it can run outside a git repo.
- Passes `--yolo` by default (use `--no-yolo` or `yolo=False` for `--full-auto`).
- Raises `RuntimeError` if Codex exits non-zero or returns no agent message.

## Configuration

Set `CODEX_BIN` to point at a non-default Codex binary:

```bash
export CODEX_BIN=/path/to/codex
```
