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

`codexapi task` exits with code 0 on success and 1 on failure.

```bash
codexapi task "Fix the failing tests." --max-iterations 5
codexapi task -f task.yaml
codexapi task -f task.yaml -i README.md
```
Create a new task file template:

```bash
codexapi create task.yaml
codexapi create my_task   # adds .yaml
```
Progress is shown by default for `codexapi task`; use `--quiet` to suppress it.
When using `--item`, the task file must include at least one `{{item}}` placeholder.

Task files default to using the standard check prompt for the task. Set `check: "None"` to skip verification.
Use `max_iterations` in the task file to override the default iteration cap (0 means unlimited).
Checks are wrapped with the verifier prompt, include the agent output, and expect JSON with `success`/`reason`.

Take tasks from a GitHub Project (requires `gh-task`):

```bash
codexapi task -p owner/projects/3 -n "Your Name" -s Ready task_a.yaml task_b.yaml
```
Reset owned tasks on a GitHub Project back to Ready:

```bash
codexapi reset -p owner/projects/3
codexapi reset -p owner/projects/3 -d   # also removes the Progress section
```

Task labels are derived from task filenames (basename without extension). The
issue title/body become `{{item}}` after removing any existing `## Progress`
section.

Example task progress run:

```bash
./examples/example_task_progress.sh
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
By default each iteration starts with a fresh Agent context; use
`--ralph-reuse` to keep a single shared context across iterations.

```bash
codexapi ralph "Fix the bug." --completion-promise DONE --max-iterations 5
codexapi ralph --ralph-reuse "Try again from the same context." --max-iterations 3
codexapi ralph --cancel --cwd /path/to/project
```

Science mode wraps a short task in a science prompt and runs it through the
Ralph loop. It defaults to `--yolo` and expects progress notes in `SCIENCE.md`.

```bash
codexapi science "hyper-optimize the kernel cycles"
codexapi science --no-yolo "hyper-optimize the kernel cycles" --max-iterations 3
```

Run a task file across a list file:

```bash
codexapi foreach list.txt task.yaml
codexapi foreach list.txt task.yaml -n 4
codexapi foreach list.txt task.yaml --retry-failed
codexapi foreach list.txt task.yaml --retry-all
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

### `task(prompt, check=None, max_iterations=10, cwd=None, yolo=True, flags=None, progress=False, set_up=None, tear_down=None, on_success=None, on_failure=None) -> str`

Runs a task with checker-driven retries and returns the success summary.
Raises `TaskFailed` when the maximum iterations are reached.

- `check` (str | None | False): custom check prompt, default checker, or `False`/`"None"` to skip.
- `max_iterations` (int): maximum number of task iterations (0 means unlimited).
- `progress` (bool): show a tqdm progress bar with a one-line status after each round.
- `set_up`/`tear_down`/`on_success`/`on_failure` (str | None): optional hook prompts.

### `task_result(prompt, check=None, max_iterations=10, cwd=None, yolo=True, flags=None, progress=False, set_up=None, tear_down=None, on_success=None, on_failure=None) -> TaskResult`

Runs a task with checker-driven retries and returns a `TaskResult` without
raising `TaskFailed`.
Arguments mirror `task()` (including hooks).

### `Task(prompt, max_iterations=10, cwd=None, yolo=True, thread_id=None, flags=None)`

Runs a Codex task with checker-driven retries. Subclass it and implement
`check()` to return an error string when the task is incomplete, or return
`None`/`""` when the task passes.
If you do not override `check()`, the default verifier wrapper runs with the
default check prompt and includes the agent output.

- `__call__(debug=False, progress=False) -> TaskResult`: run the task.
- `set_up()`: optional setup hook.
- `tear_down()`: optional cleanup hook.
- `check(output=None) -> str | None`: return an error description or `None`/`""`. `output` is the last agent response.
- `on_success(result)`: optional success hook.
- `on_failure(result)`: optional failure hook.

### `TaskResult(success, summary, iterations, errors, thread_id)`

Simple result object returned by `Task.__call__`.

- `success` (bool): whether the task completed successfully.
- `summary` (str): agent summary of what happened.
- `iterations` (int): how many iterations were used.
- `errors` (str | None): last checker error, if any.
- `thread_id` (str | None): Codex thread id for the session.

### `TaskFailed`

Exception raised by `task()` when iterations are exhausted.

- `summary` (str): failure summary text.
- `iterations` (int | None): iterations made when the task failed.
- `errors` (str | None): last checker error, if any.

### `foreach(list_file, task_file, n=None, cwd=None, yolo=True, flags=None) -> ForeachResult`

Runs a task file over a list of items, updating the list file in place.

- `list_file` (str | PathLike): path to the list file to process.
- `task_file` (str | PathLike): YAML task file (must include `prompt`).
- `n` (int | None): limit parallelism to N (default: run all items in parallel).
- `cwd` (str | PathLike | None): working directory for the Codex session.
- `yolo` (bool): pass `--yolo` to Codex when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to Codex.

### `ForeachResult(succeeded, failed, skipped, results)`

Simple result object returned by `foreach()`.

- `succeeded` (int): number of successful items.
- `failed` (int): number of failed items.
- `skipped` (int): number of items skipped (already marked in the list file).
- `results` (list[tuple]): `(item, success, summary)` entries for items that ran.

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
