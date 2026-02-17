# Lead Example: Blocked Worker

This example creates a tiny process that starts blocked and will not finish until the lead resolves it.
The lead's job is to observe, diagnose, and drive it to completion.

## Run It

```bash
./examples/lead/run_example.sh
```

Notes:
- `codexapi lead` will create `examples/lead/LEADBOOK.md` automatically.
- Clean artifacts with `./examples/lead/clean.sh`.

The lead should find the worker's log in `examples/lead/state/worker.log`, see
that it is blocked, and figure out what needs to happen for completion. A
successful run results in a `worker.done` file.
