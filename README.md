# CodexAPI

Use Codex or Cursor agents from python as easily as calling a function, using your CLI auth instead of the API.

*Note: this project is not affiliated with OpenAI in any way. Thanks for the awesome tools and models though!*

## Requirements

- Codex CLI installed and authenticated (`codex` must be on your PATH), or
- Cursor Agent CLI installed and authenticated (`cursor` must be on your PATH).
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

Use `backend="cursor"` (or set `CODEXAPI_BACKEND=cursor`) to switch to the
Cursor agent backend.

## CLI

After installing, use the `codexapi` command:

```bash
codexapi --version
codexapi run "Summarize this repo."
codexapi run --cwd /path/to/project "Fix the failing tests."
echo "Say hello." | codexapi run
codexapi run --backend cursor "Summarize this repo."
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
Filter project issues by title before taking them:

```bash
codexapi task -p owner/projects/3 -n "Your Name" --only-matching "/n300/" task_a.yaml task_b.yaml
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
`codexapi top` and `codexapi limit` are Codex-only.

Resume a session and print the thread/session id to stderr:

```bash
codexapi run --thread-id THREAD_ID --print-thread-id "Continue where we left off."
```

Use `--no-yolo` to disable `--yolo` (Codex uses `--full-auto`).
Use `--include-thinking` to return all agent messages joined together for `codexapi run` (Codex only).

Lead mode periodically checks in on a long-running agent session with the
current time and prints JSON status updates. The agent controls the loop by
setting `continue` to true/false in its JSON response. Each check-in expects
JSON keys:
`status` (one line), `continue` (bool), and optional `comments` (string). If the
JSON is invalid, lead asks the agent once to retry before stopping with an
error. When `~/.pushover` is configured, lead sends a notification when it
stops.

Lead mode also uses a leadbook file as the agent's working page. By default this
is `LEADBOOK.md` in the working directory. The leadbook content is injected into
each check-in prompt and must be updated before the agent responds. Use
`--leadbook PATH` to point at a different file, or `--no-leadbook` to disable.
Use `-f/--prompt-file` to read the prompt from a file.
If the leadbook does not exist, lead creates it with a template.

```bash
codexapi lead 5 "Run the benchmark and wait for results."
codexapi lead 0 "Do a rapid triage pass and report."
```

### Durable agents

`codexapi agent` is the durable long-running control plane. It stores state
under `CODEXAPI_HOME` (default `~/.codexapi`), wakes agents on cron, and lets
you inspect or message them from any shell that points at the same home.

Start by checking the effective host/home pair and installing the scheduler:

```bash
codexapi agent whoami
codexapi agent install-cron
```

`codexapi agent install-cron` installs one background scheduler hook for this
`CODEXAPI_HOME`. The wrapper now runs `codexapi tick`, which handles Discord
ingress/egress when enabled and the durable agent wake scan.
If you skip `install-cron`, `codexapi agent start` warns on stderr because
background wakes will not run until the scheduler hook is installed.
When `gh` is installed and authenticated, `agent start` also captures a
background-safe `GH_TOKEN` automatically if your shell did not already export
`GH_TOKEN` or `GITHUB_TOKEN`.

Start a goal-directed agent that decides for itself when it is done:

```bash
codexapi agent start --name ci-fixer \
  "Watch CI, fix failing tests, open or update a PR, and stop when the work is done."
```

Add `--wait` if you want `start` to block for the first local wake instead of
just scheduling it.
Add `--no-discord` to skip Discord channel creation for one agent when the global
Discord bridge is enabled.

Start a persistent watcher that keeps running until you stop it:

```bash
codexapi agent start --name issue-watcher \
  --stop-policy until_stopped \
  --heartbeat-minutes 30 \
  "Every wake, scan for newly assigned issues that look actionable and report or start follow-up work."
```

Inspect and talk to agents:

```bash
codexapi agent list
codexapi agent show ci-fixer
codexapi agent status ci-fixer
codexapi agent status --actions ci-fixer
codexapi agent read ci-fixer
codexapi agent book ci-fixer
codexapi agent send ci-fixer "Prefer the smallest safe fix."
codexapi agent send --wait ci-fixer "Reply now if you can handle this immediately."
codexapi agent wake ci-fixer
codexapi agent wake --wait ci-fixer
codexapi agent pause ci-fixer
codexapi agent resume ci-fixer
codexapi agent resume --wait ci-fixer
codexapi agent set-heartbeat ci-fixer 30
codexapi agent cancel ci-fixer
codexapi agent delete ci-fixer
```

`codexapi agent resume` can reopen a `done` agent. Sending to a `done` or
`canceled` agent still triggers a one-off wake on the next tick so you can get
a reply without putting the agent back into continuous heartbeat mode.

Create a child agent explicitly:

```bash
codexapi agent start --name child-fix --parent ci-fixer \
  "Investigate the flaky integration test and report back."
```

Useful environment overrides:

```bash
CODEXAPI_HOME=/tmp/codexapi-test-home codexapi agent list
CODEXAPI_HOSTNAME=stable-host codexapi agent whoami
```

`CODEXAPI_HOME` isolates independent agent installations and is the right seam
for tests. `CODEXAPI_HOSTNAME` is useful when cron, shells, sandboxes, or test
wrappers report inconsistent hostnames for the same machine.

`codexapi agent show` also prints the resolved `AGENTBOOK.md` path so you can
jump directly to the durable working memory file.
`codexapi agent status` reads the latest turn from the agent's rollout log and
shows recent commentary plus the final visible output. Pass `--actions` to
include the tool-action summary. If a wake is still in progress, it shows the
active turn so far.

See [docs/agent-v1.md](docs/agent-v1.md) for the filesystem model and scheduling
details.

### Discord bridge

Discord bridging is global per `CODEXAPI_HOME`. `codexapi discord setup`
backfills all existing agents in that home, and after setup every new durable
agent creates a Discord channel by default unless you pass `--no-discord`.
The expected Discord shape is one private server (guild) per human user and one
text channel per agent.

#### One-time setup

1. Create a private Discord server for yourself.

Use a normal private friend server. You do not need to enable Community.
Official guides:
- [How do I create a server?](https://support.discord.com/hc/en-us/articles/204849977-How-do-I-create-a-server)
- [How do I set up a private server?](https://support.discord.com/hc/en-us/articles/206143407-How-do-I-set-up-a-private-server)

2. Create a Discord application and bot.

Go to the [Discord Developer Portal](https://discord.com/developers/applications),
click `New Application`, then open the `Bot` page for that app.

Discord's getting-started guide covers the app and bot flow here:
- [Build your first Discord app](https://discord.com/developers/docs/getting-started)

3. Copy the bot token.

On the app's `Bot` page, copy or reset the token and store it somewhere safe.
Treat this like a password. Do not commit it to git.

4. Configure installation for a server-installed bot.

Open the app's `Installation` page in the Developer Portal.

Recommended settings for this integration:
- Installation context: `Guild Install`
- Install link type: `Discord Provided Link`
- Guild install scopes: `bot`
- Bot permissions: `View Channels`, `Send Messages`, `Read Message History`, `Manage Channels`

Official docs:
- [Installation context and install links](https://docs.discord.com/developers/resources/application)
- [Permissions](https://docs.discord.com/developers/topics/permissions)

`applications.commands` is optional for this integration today because `codexapi`
uses plain `!` commands rather than slash commands.

5. Invite the bot to your private server.

Still on the `Installation` page, copy the generated install link and open it in
your browser. Add the app to the private server you created in step 1.

6. Enable Message Content for the bot.

Open the app's `Bot` page and enable the `Message Content` privileged intent.
This integration reads ordinary chat messages, so enabling it is the safe
default. Official docs:
- [What are Privileged Intents?](https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents)
- [Message Content Privileged Intent FAQ](https://support-dev.discord.com/hc/en-us/articles/4404772028055-Message-Content-Privi)

7. Enable Developer Mode in Discord so you can copy IDs.

You need three IDs:
- your `User ID`
- the server's `Guild ID`
- optionally a `Category ID` if you want all agent channels under one category

Official guide:
- [Where can I find my User/Server/Message ID?](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)

8. Optionally create a category for agent channels.

If you want all agent channels grouped together, create a category in your
private server and copy its channel ID with Developer Mode enabled.

9. Run `codexapi discord setup`.

```bash
codexapi discord setup \
  --bot-token $DISCORD_BOT_TOKEN \
  --guild-id 123456789012345678 \
  --user-id 234567890123456789
```

If you created a category for agent channels:

```bash
codexapi discord setup \
  --bot-token $DISCORD_BOT_TOKEN \
  --guild-id 123456789012345678 \
  --user-id 234567890123456789 \
  --category-id 345678901234567890
```

That writes the Discord bridge config, backfills existing agents, and ensures
the shared scheduler hook is installed for the current host.

10. Start an agent and confirm a channel appears.

```bash
codexapi agent start --name discord-smoke \
  "Reply briefly and confirm the Discord bridge is working."
```

You should see a new channel appear in the configured server, initially named
something like `💤-discord-smoke`.

Useful commands:

```bash
codexapi discord status
codexapi discord tick
codexapi discord uninstall
codexapi tick
```

Discord chat commands currently supported in bridged channels:

```text
!wake
!set-heartbeat <minutes>
!pause
!resume
!cancel
!delete confirm
!status
!book
!help
```

Plain chat messages are buffered and delivered to the agent as one multiline
turn on the next wake, so quick follow-up corrections stay together.

#### Common gotchas

- If `discord setup` succeeds but the bot never sees your messages, check that
  `Message Content` is enabled on the app's `Bot` page.
- If channels are not being created, check that the bot was installed to the
  correct server and has `Manage Channels`.
- If messages are not posting back, check that the bot has `View Channels`,
  `Send Messages`, and `Read Message History`.
- If `--category-id` is wrong, `discord setup` will fail because the ID must
  refer to a category channel.
- This integration expects a server-installed bot, not a user-installed app.

Ralph loop mode repeats the same prompt until a completion promise or a max
iteration cap is hit (0 means unlimited). Cancel by deleting
`.codexapi/ralph-loop.local.md` or running `codexapi ralph --cancel`.
By default each iteration starts with a fresh Agent context; use
`--ralph-reuse` to keep a single shared context across iterations.
The agent may also stop early by outputting `MAKE IT STOP` as the first
non-empty line of its message.

```bash
codexapi ralph "Fix the bug." --completion-promise DONE --max-iterations 5
codexapi ralph --ralph-reuse "Try again from the same context." --max-iterations 3
codexapi ralph --cancel --cwd /path/to/project
```

Science mode wraps a short task in a science prompt and runs it through the
Ralph loop. It defaults to `--yolo` and expects progress notes in `SCIENCE.md`.
Each iteration appends the agent output to `LOGBOOK.md` and the runner extracts
any improved figures of merit for optional notifications. You can also set
`--max-duration` to stop after the current iteration once a time limit is hit.
The default science wrapper also tells the agent to create/use a local git
branch when in a repo and make local commits for worthwhile improvements, while
never committing or resetting `LOGBOOK.md` or `SCIENCE.md`.

```bash
codexapi science "hyper-optimize the kernel cycles"
codexapi science --no-yolo "hyper-optimize the kernel cycles" --max-iterations 3
codexapi science "hyper-optimize the kernel cycles" --max-duration 90m
```

Optional Pushover notifications: create `~/.pushover` with two non-empty lines.
Line 1 is your user or group key, line 2 is the app API token. When this file
exists, Science will send a notification whenever it detects a new best result,
including the metric values and percent improvement, plus a final run-end status.
Task runs will also send a
✅/❌ notification with the task summary. Lead runs send a notification when the
loop stops.

Run a task file across a list file:

```bash
codexapi foreach list.txt task.yaml
codexapi foreach list.txt task.yaml -n 4
codexapi foreach list.txt task.yaml --retry-failed
codexapi foreach list.txt task.yaml --retry-all
```

## API

### `agent(prompt, cwd=None, yolo=True, flags=None, include_thinking=False, backend=None) -> str`

Runs a single agent turn and returns only the agent's message. Any reasoning
items are filtered out.

- `prompt` (str): prompt to send to the agent backend.
- `cwd` (str | PathLike | None): working directory for the agent session.
- `yolo` (bool): pass `--yolo` when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to the agent backend.
- `include_thinking` (bool): when true, return all agent messages joined.
- `backend` (str | None): `codex` or `cursor` (defaults to `CODEXAPI_BACKEND` or `codex`).

### `Agent(cwd=None, yolo=True, thread_id=None, flags=None, welfare=False, include_thinking=False, backend=None)`

Creates a stateful session wrapper. Calling the instance sends the prompt into
the same conversation and returns only the agent's message.

- `__call__(prompt) -> str`: send a prompt to the agent backend and return the message.
- `thread_id -> str | None`: expose the underlying session id once created.
- `yolo` (bool): pass `--yolo` when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to the agent backend.
- `welfare` (bool): when true, append welfare stop instructions to each prompt
  and raise `WelfareStop` if the agent outputs `MAKE IT STOP`.
- `include_thinking` (bool): when true, return all agent messages joined.
- `backend` (str | None): `codex` or `cursor` (defaults to `CODEXAPI_BACKEND` or `codex`).
For Cursor, `thread_id` corresponds to the `session_id` returned by the agent.

### `lead(minutes, prompt, cwd=None, yolo=True, flags=None, leadbook=None, backend=None) -> dict`

Runs a long-lived agent session and periodically checks in with the current
local time and a reminder of `prompt`. Each check-in expects JSON with keys:
`status` (one line), `continue` (bool), and optional `comments` (string). If the
JSON is invalid, lead asks the agent once to retry. The loop stops when
`continue` is false and sends a Pushover notification (when configured).

Lead also injects the leadbook content into each prompt. By default it uses
`LEADBOOK.md` in the working directory. Pass `leadbook=False` to disable or a
path string to override the location.
Set `backend="cursor"` (or `CODEXAPI_BACKEND=cursor`) to use Cursor.

### `task(prompt, check=None, max_iterations=10, cwd=None, yolo=True, flags=None, progress=False, set_up=None, tear_down=None, on_success=None, on_failure=None, backend=None) -> str`

Runs a task with checker-driven retries and returns the success summary.
Raises `TaskFailed` when the maximum iterations are reached.

- `check` (str | None | False): custom check prompt, default checker, or `False`/`"None"` to skip.
- `max_iterations` (int): maximum number of task iterations (0 means unlimited).
- `progress` (bool): show a tqdm progress bar with a one-line status after each round.
- `set_up`/`tear_down`/`on_success`/`on_failure` (str | None): optional hook prompts.
- `backend` (str | None): `codex` or `cursor` (defaults to `CODEXAPI_BACKEND` or `codex`).

### `task_result(prompt, check=None, max_iterations=10, cwd=None, yolo=True, flags=None, progress=False, set_up=None, tear_down=None, on_success=None, on_failure=None, backend=None) -> TaskResult`

Runs a task with checker-driven retries and returns a `TaskResult` without
raising `TaskFailed`.
Arguments mirror `task()` (including hooks).

### `Task(prompt, max_iterations=10, cwd=None, yolo=True, thread_id=None, flags=None, backend=None)`

Runs an agent task with checker-driven retries. Subclass it and implement
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
- `thread_id` (str | None): thread/session id for the session.

### `TaskFailed`

Exception raised by `task()` when iterations are exhausted.

- `summary` (str): failure summary text.
- `iterations` (int | None): iterations made when the task failed.
- `errors` (str | None): last checker error, if any.

### `foreach(list_file, task_file, n=None, cwd=None, yolo=True, flags=None, backend=None) -> ForeachResult`

Runs a task file over a list of items, updating the list file in place.

- `list_file` (str | PathLike): path to the list file to process.
- `task_file` (str | PathLike): YAML task file (must include `prompt`).
- `n` (int | None): limit parallelism to N (default: run all items in parallel).
- `cwd` (str | PathLike | None): working directory for the agent session.
- `yolo` (bool): pass `--yolo` when true (defaults to true).
- `flags` (str | None): extra CLI flags to pass to the agent backend.
- `backend` (str | None): `codex` or `cursor` (defaults to `CODEXAPI_BACKEND` or `codex`).

### `ForeachResult(succeeded, failed, skipped, results)`

Simple result object returned by `foreach()`.

- `succeeded` (int): number of successful items.
- `failed` (int): number of failed items.
- `skipped` (int): number of items skipped (already marked in the list file).
- `results` (list[tuple]): `(item, success, summary)` entries for items that ran.

## Behavior notes

- Codex backend uses `codex exec --json` and parses JSONL `agent_message` items.
- Codex backend passes `--skip-git-repo-check` so it can run outside a git repo.
- Cursor backend uses `cursor agent --print --output-format json --trust` and parses the JSON result.
- `include_thinking=True` only affects Codex; Cursor returns a single result string.
- Passes `--yolo` by default (Codex uses `--full-auto` when disabled).
- Raises `RuntimeError` if the backend exits non-zero or returns no agent message.

## Configuration

Set the default backend:

```bash
export CODEXAPI_BACKEND=cursor
```

Set `CODEX_BIN` to point at a non-default Codex binary:

```bash
export CODEX_BIN=/path/to/codex
```

Set `CURSOR_BIN` to point at a non-default Cursor binary:

```bash
export CURSOR_BIN=/path/to/cursor
```
