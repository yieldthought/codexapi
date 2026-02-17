# Lead Example: Blocked Worker

This example creates a tiny process that should complete and write a done marker.
The lead's job is to observe, diagnose, and drive it to completion.

## Run It

1. Start the worker:

```bash
cd ./examples/lead
./start_worker.sh
```

2. In another terminal, start the lead loop:

```bash
cd ./examples/lead
codexapi lead 1 -f prompt.txt
```

Or run both steps in one go:

```bash
./examples/lead/run_example.sh
```

Notes:
- `codexapi lead` will create `examples/lead/LEADBOOK.md` automatically.

The lead should find the worker's log in `examples/lead/state/worker.log` and
figure out what needs to happen for completion. A successful run results in a
`worker.done` file.
