#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$root/start_worker.sh"

echo "Starting codexapi lead..."
cd "$root"
codexapi lead 1 -f prompt.txt
