#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
state="$root/state"

rm -rf "$state"
mkdir -p "$state"

fifo="$state/input.fifo"
mkfifo "$fifo"

echo "Starting worker..."
python3 "$root/worker.py" "$fifo" > "$state/worker.log" 2>&1 &
echo $! > "$state/worker.pid"

echo "Worker pid: $(cat "$state/worker.pid")"

cat <<EOF2

Example is running.
- Logs: $state/worker.log
- FIFO: $state/input.fifo
- Done marker: $state/worker.done

Run the lead loop in another terminal:
  cd "$root"
  codexapi lead 1 -f prompt.txt

EOF2
