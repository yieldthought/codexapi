# codexapi agent V1

## Purpose

`codexapi agent` is a long-term fire-and-forget orchestration layer built on top
of the existing agent, task, science, and lead primitives.

The V1 goal is not to invent a new kind of coding agent. The goal is to make a
durable agent that can:

- keep working for days
- survive sleep, reboot, and missed scheduler runs
- be inspected and controlled from the CLI
- accept messages while it is running
- delegate coding work to `codexapi task` or `codexapi science`
- escalate to the user when needed

The design is intentionally simple. It uses durable filesystem state plus one
periodic scheduler entry per `CODEXAPI_HOME`.

## Non-Goals

V1 does not try to solve everything.

- No daemon is required.
- No SSH is required.
- No cross-host migration or "teleportation" of running agents.
- No separate task-agent and watcher-agent runtimes.
- No catch-up replay of missed heartbeat ticks.
- No dependence on real cron in automated tests.
- No shared append-only logs written by multiple hosts.

These are deliberate omissions. They keep the system small, portable, and easy
to reason about.

## Top-Level Model

An agent is a durable record plus a periodic wake mechanism.

- There is one agent type.
- Each agent has a `stop_policy`.
- Each agent belongs to exactly one `CODEXAPI_HOME`.
- Each agent is owned by exactly one hostname.
- Only the owning hostname may wake and run the agent.
- Any host that can see the shared filesystem may inspect the agent and queue
  commands for it.

The agent's durable truth is the state stored under `CODEXAPI_HOME`, not a live
backend process. Each wake starts a fresh backend process and resumes from the
saved thread id when available.

## `CODEXAPI_HOME`

`CODEXAPI_HOME` is the root of a complete agent control plane.

Default:

```text
~/.codexapi
```

Override:

```text
CODEXAPI_HOME=/path/to/home
```

Why this exists:

- It isolates live state from tests.
- It allows multiple independent codexapi installations on one machine.
- It allows a shared filesystem setup without forcing all state into one global
  namespace.

Two different `CODEXAPI_HOME` values are two different systems. They do not see
each other's agents, locks, scheduler wrappers, or cron entries.

## Agent Model

Each agent stores at least:

- `id`: stable identifier
- `name`: human-readable unique name within the home
- `created_at`: UTC timestamp
- `created_by`: user name or parent agent name
- `hostname`: owning host for execution
- `cwd`: working directory
- `prompt`: original instruction text
- `stop_policy`: `until_done` or `until_stopped`
- `status`: current lifecycle state
- `thread_id`: backend resume id, or empty
- `heartbeat_minutes`: heartbeat interval
- `last_wake_at`: last attempted wake time in UTC
- `last_success_at`: last completed wake time in UTC
- `next_wake_at`: next heartbeat due time in UTC
- `wake_requested_at`: durable "run soon" flag for queued commands/messages
- `unread_message_count`: messages not yet folded into a wake
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `avg_tokens_per_hour`
- `child_ids`
- `last_error`: most recent failure summary, if any
- `activity`: short status text for `agent list`

V1 uses one agent type with one explicit lifecycle hint:

- `stop_policy=until_done`: agent is expected to decide when it is finished
- `stop_policy=until_stopped`: agent is expected to keep running until stopped

This keeps the runtime unified while preserving a small but important semantic
difference for scheduling and UI.

## Lifecycle States

V1 keeps the state model small:

- `ready`: can be woken when due
- `running`: a wake is currently in progress
- `paused`: do not wake until resumed
- `done`: completed by the agent's own judgment
- `canceled`: stopped by an explicit command
- `error`: last wake failed and the agent needs attention or another wake

Why these states:

- `ready` and `running` are enough for normal operation
- `paused`, `done`, and `canceled` are user-visible terminal or semi-terminal
  control states
- `error` makes failures explicit without inventing a richer failure taxonomy

## Filesystem Layout

All paths below are relative to `CODEXAPI_HOME`.

```text
agents/
  <agent_id>/
    meta.json
    state.json
    AGENTBOOK.md
    commands/
      new/
      claimed/
    hosts/
      <hostname>/
        session.json
        run.lock
        runs/
locks/
  .tick.<hostname>.lock
bin/
  agent-tick
cron/
  agent.cron
```

### `agents/<agent_id>/meta.json`

Purpose:
- Stable identity and configuration.

Writer:
- Owner host only after agent creation, except for explicit configuration
  changes.

Readers:
- Any host.

Format:
- JSON object.

Why it exists:
- Separates mostly-static configuration from rapidly changing state.

Suggested contents:
- `id`, `name`, `created_at`, `created_by`, `hostname`, `cwd`, `prompt`,
  `stop_policy`, `heartbeat_minutes`

### `agents/<agent_id>/state.json`

Purpose:
- Current snapshot for CLI inspection.

Writer:
- Owner host only.

Readers:
- Any host.

Format:
- JSON object rewritten atomically with temp file + rename.

Why it exists:
- `agent list` and `agent show` should not need to reconstruct state from many
  files or logs.

Suggested contents:
- `status`, `thread_id`, `last_wake_at`, `last_success_at`, `next_wake_at`,
  `wake_requested_at`, `unread_message_count`, token totals, `activity`,
  `last_error`, `child_ids`

### `agents/<agent_id>/AGENTBOOK.md`

Purpose:
- Human-readable working memory for the agent, similar to the leadbook.

Writer:
- Owner host only.

Readers:
- Any host.

Format:
- Markdown.

Why it exists:
- Thread ids are not sufficient durable memory. The book is the portable,
  inspectable memory surface.

### `agents/<agent_id>/commands/new/`

Purpose:
- Durable cross-host command spool.

Writer:
- Any host may create new files here.

Readers:
- Owner host only for processing, any host for debugging.

Format:
- One JSON file per command.

Why it exists:
- It avoids shared append logs and avoids requiring SSH or direct host
  reachability.

Filename rule:

```text
<utc>.<origin-host>.<pid>.<random>.json
```

Writers must:

- write to a temp file in the same directory tree
- `fsync` if practical
- rename atomically into `commands/new/`

Supported V1 commands:

- `send`
- `wake`
- `pause`
- `resume`
- `cancel`

### `agents/<agent_id>/commands/claimed/`

Purpose:
- Temporary processing area for commands taken by the owner host.

Writer:
- Owner host only.

Readers:
- Mainly owner host; other hosts may inspect for debugging.

Format:
- Same JSON command files, moved from `new/`.

Why it exists:
- Claim-by-rename is simple, durable, and avoids double processing.

After a claimed command is applied, the owner host should record the outcome in
`state.json` or a run record and then remove the command file. The command file
is transport, not long-term audit storage.

### `agents/<agent_id>/hosts/<hostname>/session.json`

Purpose:
- Host-local runtime data for the owner host.

Writer:
- Owner host only.

Readers:
- Mostly owner host.

Format:
- JSON object.

Why it exists:
- Keeps the liveliest mutable runtime fields under a host-specific path.

Suggested contents:
- `thread_id`
- environment snapshot used for execution
- last run metadata that does not need to be duplicated in `state.json`

### `agents/<agent_id>/hosts/<hostname>/run.lock`

Purpose:
- Non-blocking per-agent run lock.

Writer:
- Owner host only.

Readers:
- Owner host only in normal operation.

Format:
- Permanent lock file used with `flock` or `fcntl`.

Why it exists:
- Prevents two entry points from resuming the same backend thread at the same
  time.

### `agents/<agent_id>/hosts/<hostname>/runs/`

Purpose:
- Per-wake run records for debugging and recovery.

Writer:
- Owner host only.

Readers:
- Any host.

Format:
- One JSON file per wake.

Why it exists:
- Per-run files are easier to inspect and safer than multi-host append logs.

Suggested contents:
- start and end times
- reason for wake
- commands consumed
- agent reply text or status payload intended for the CLI
- token deltas
- result summary
- error details if any

### `bin/agent-tick`

Purpose:
- Stable wrapper script for cron.

Writer:
- `codexapi agent install-cron`

Readers:
- Cron and the user.

Format:
- Executable shell script.

Why it exists:
- Cron has a sparse environment. The wrapper pins the interpreter and exports a
  safe environment.

The wrapper should:

- export the resolved `CODEXAPI_HOME`
- set a safe `PATH`
- invoke the exact Python interpreter or installed `codexapi` path discovered
  at install time

### `cron/agent.cron`

Purpose:
- Record of the cron line managed for this `CODEXAPI_HOME`.

Writer:
- `codexapi agent install-cron`

Readers:
- User and installer commands.

Format:
- Plain text.

Why it exists:
- Makes scheduler installation inspectable and testable without reading the
  user's entire crontab.

## Ownership Rules

The design is intentionally asymmetric.

- Any host may read any agent in the same `CODEXAPI_HOME`.
- Only the owner host may run the agent.
- Any host may enqueue command files in `commands/new/`.
- Only the owner host may mutate `state.json`, `AGENTBOOK.md`, host runtime
  files, and run records.

Why this matters:

- It keeps cross-host writes minimal.
- It avoids shared append logs.
- It allows one shared registry across machines without letting an agent wake on
  the wrong host.

## Scheduler

V1 uses exactly one cron entry per `CODEXAPI_HOME` and per host.

Cron cadence:

- every minute

Cron target:

- `CODEXAPI_HOME/bin/agent-tick`

Why one scheduler entry:

- one place to reason about wake behavior
- no per-agent cron management
- easy recovery after reboot or sleep

Why cron:

- available on macOS and Linux
- no root requirement
- simple installation story

## Tick Lock

Each host uses a host-specific scheduler lock:

```text
locks/.tick.<hostname>.lock
```

Locking rules:

- lock acquisition is non-blocking
- if the lock is held, `codexapi agent tick` exits `0` immediately
- missed scheduler invocations are dropped, not queued

The lock file itself may contain debug text such as pid and start time, but the
authority is the kernel file lock, not file existence.

Why this matters:

- a long tick must not cause future ticks to pile up
- crash recovery is automatic because kernel locks are released when the process
  dies

## Per-Agent Run Lock

Each agent has its own non-blocking run lock under its owner host directory.

Rules:

- `tick`, `send`, and any future explicit wake path must all respect this lock
- if the lock is held, the caller must not wait
- if new commands arrive while the agent is running, they stay queued for the
  next wake

Why this matters:

- one backend process per agent
- no concurrent `resume` on the same thread id

## Tick Semantics

`codexapi agent tick` should:

1. resolve `CODEXAPI_HOME`
2. resolve the current hostname
3. take the host-specific tick lock or exit `0`
4. scan all agents in this home
5. ignore agents whose owner hostname does not match
6. select agents that are due
7. try each due agent with its non-blocking run lock

An agent is due when all of the following are true:

- `status` is `ready` or `error`
- owner hostname matches the current hostname
- one of:
  - `wake_requested_at` is set
  - unread commands/messages exist
  - `next_wake_at` is present and in the past

Heartbeat behavior:

- missed heartbeat opportunities are dropped
- there is no replay of missed intervals after sleep or reboot
- the next heartbeat is scheduled from the time the current wake finishes, not
  from the last planned heartbeat slot

Why this matters:

- heartbeats are a chance to check in, not a durable queue
- durable user intent must live in command files, not in hypothetical missed
  ticks

## Command Processing

Command files are the durable cross-host control plane.

Suggested command shape:

```json
{
  "id": "20260306T211500Z.host.pid.abcd",
  "created_at": "2026-03-06T21:15:00Z",
  "origin_hostname": "workstation-a",
  "kind": "send",
  "body": "Status?",
  "author": "mark"
}
```

Processing rules:

- owner host claims commands by rename from `new/` to `claimed/`
- commands are applied in timestamp order
- `pause` and `cancel` are applied before starting a new backend wake
- `send` contributes to the next prompt and increments unread counts until
  consumed
- `wake` means run soon even if no heartbeat is due
- `resume` only changes state when the agent is paused
- after successful application, the owner host records the result in state or a
  run record and deletes the claimed file

Why command files instead of SSH:

- durable when the owner host is asleep or unreachable
- portable
- fewer assumptions about local network setup

## Wakes and Backend Process Model

Each wake is a fresh backend process.

Rules:

- do not keep a `codex` process alive between heartbeats
- when a wake starts, resume from `thread_id` if present
- when the wake ends, persist the updated `thread_id`
- if no `thread_id` exists, start a fresh thread

Why this matters:

- robust to reboot and crash
- simpler process management
- clearer token accounting per wake

The backend thread id is useful memory, but not the source of truth. Durable
memory lives in the agent home, especially `state.json`, command files, and
`AGENTBOOK.md`.

## Environment Handling

The scheduler environment and the agent execution environment are not assumed to
be the same.

Each agent should persist enough environment to resume sanely:

- `cwd`
- `PATH`
- `VIRTUAL_ENV`, if set
- interpreter path used to launch codexapi-related subprocesses when relevant

Why this matters:

- the cron-driven scheduler may run from a different venv than the one the user
  had active when the agent was created
- repo commands like `python`, `pytest`, and tool wrappers often depend on
  `PATH` and `VIRTUAL_ENV`

V1 should store only the minimum needed to recreate the expected environment.

## Token Accounting

V1 should not pretend to know dollar cost.

Track:

- `input_tokens`
- `output_tokens`
- `total_tokens`
- `avg_tokens_per_hour`

Token totals belong in `state.json` so `agent list` can show them cheaply.

Why this matters:

- heartbeat-heavy agents can become unexpectedly expensive in quota terms
- users need a simple proxy for long-running agent cost

`avg_tokens_per_hour` is a lifetime running average in V1. More detailed recent
windows can be added later if needed.

## CLI Contract

V1 CLI surface:

- `codexapi agent start`
- `codexapi agent list`
- `codexapi agent read`
- `codexapi agent show`
- `codexapi agent send`
- `codexapi agent wake`
- `codexapi agent pause`
- `codexapi agent resume`
- `codexapi agent cancel`
- `codexapi agent tick`
- `codexapi agent install-cron`

Expected behavior:

- `start` creates the agent directory, meta/state files, and host runtime files
- `list` reads only this `CODEXAPI_HOME`
- `read` shows recent user-visible communication derived from state and run
  records
- `show` reads one agent's current snapshot and recent run history
- `send`, `wake`, `pause`, `resume`, and `cancel` create durable command files
- `tick` processes due agents for the current hostname only
- `install-cron` installs exactly one scheduler entry for this home on this host

Why command-oriented CLI actions:

- one path for local and cross-host control
- durable intent
- simpler concurrency model

## Failure Recovery

V1 should explicitly recover from common failure modes.

### Reboot or Sleep

- missed cron minutes are ignored
- the next cron minute runs `agent tick`
- due agents are selected from current state, not from queued heartbeat ticks

### Tick Crash

- kernel lock is released when the tick process dies
- next cron minute may run normally

### Wake Crash

- per-agent run lock is released when the process dies
- next tick sees the agent is not actually locked
- if `state.json` still says `running`, reconcile it to `error` or `ready`
  before proceeding

### Owner Host Unavailable

- other hosts may still inspect the agent and enqueue commands
- commands remain durable until the owner host comes back

## Testing Strategy

The main automated testing tool is a temporary `CODEXAPI_HOME`.

Why this is the right testing seam:

- it isolates tests from live agents
- it allows end-to-end command and tick tests without real cron
- it matches the real control-plane boundary

### Test Rules

- every integration test sets `CODEXAPI_HOME` to a temp directory
- tests call CLI commands or internal functions directly
- tests run `codexapi agent tick` directly instead of invoking cron
- tests must never depend on the default `~/.codexapi`

### Test Layers

Unit tests:

- due-agent selection
- heartbeat scheduling
- token accounting
- path resolution
- command parsing
- state transition logic

Filesystem integration tests:

- create an agent
- enqueue command files
- run `tick`
- verify command consumption, state updates, and next wake times
- verify that different `CODEXAPI_HOME` roots are fully isolated

Backend-stub integration tests:

- replace real backend execution with a fake runner
- return canned outputs and thread ids
- verify prompt construction, session resume, and token accounting

Scheduler tests:

- verify wrapper script generation
- verify cron line rendering
- verify that two different `CODEXAPI_HOME` roots on one host produce separate
  scheduler artifacts
- do not touch a real user crontab in normal automated tests

Cross-host tests:

- fake different hostnames
- verify that only the owner hostname wakes an agent
- verify that non-owner hosts can still enqueue commands

Locking tests:

- simulate tick lock contention and assert fast `0` exit
- simulate per-agent run lock contention and assert no second wake starts

## Invariants

These are the rules the implementation should preserve.

- `CODEXAPI_HOME` is a complete isolated control plane.
- One cron installation belongs to one home on one host.
- Only the owner hostname may wake an agent.
- Heartbeat opportunities are lossy.
- Commands are durable.
- No caller waits on the tick lock or per-agent run lock.
- There is never more than one live backend process per agent.
- The backend thread id is useful state, not the source of truth.
- Cross-host writes use one-file command spooling, not shared append logs.
- Agent state shown in the CLI comes from `state.json`, not from expensive live
  reconstruction.

## Why This Design Is Small Enough

This design deliberately avoids many attractive additions.

- It does not require a daemon.
- It does not require host-to-host RPC.
- It does not require richer distributed locking than the filesystem already
  provides.
- It does not require a database.

What remains is the minimum necessary structure for a durable, inspectable,
multi-day agent system:

- one state root
- one scheduler entry
- one agent directory per agent
- one command spool per agent
- one host owner per agent
- one run at a time

That is a good V1 shape.
