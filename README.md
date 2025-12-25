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
from codexapi import agent, Agent

# Run one-shot tasks as a function call
print(agent("Say hello"))

# Run a multi-turn conversation as a session
session = Agent(cwd="/path/to/project")
print(session("Summarize this repo."))
print(session("Now list any risks."))

# Save and resume a session later
thread_id = session.thread_id
session2 = Agent(cwd="/path/to/project", trace_id=thread_id)
print(session2("Continue from where we left off."))
```

## API

### `agent(prompt, cwd=None, *, yolo=False, agent="codex", flags=None) -> str`

Runs a single Codex turn and returns only the agent's message. Any reasoning
items are filtered out.

- `prompt` (str): prompt to send to Codex.
- `cwd` (str | PathLike | None): working directory for the Codex session.
- `yolo` (bool): pass `--yolo` to Codex when true.
- `agent` (str): agent backend to use (only `"codex"` is supported).
- `flags` (str | None): extra CLI flags to pass to Codex.

### `Agent(cwd=None, *, yolo=False, agent="codex", trace_id=None, flags=None)`

Creates a stateful session wrapper. Calling the instance sends the prompt into
the same conversation and returns only the agent's message.

- `__call__(prompt) -> str`: send a prompt to Codex and return the message.
- `thread_id -> str | None`: expose the underlying session id once created.
- `trace_id` (str | None): Codex thread id to resume from the first call.
- `yolo` (bool): pass `--yolo` to Codex when true.
- `agent` (str): agent backend to use (only `"codex"` is supported).
- `flags` (str | None): extra CLI flags to pass to Codex.

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
