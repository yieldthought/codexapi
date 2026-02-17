#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rm -rf "$root/state"
rm -f "$root/LEADBOOK.md"

echo "Cleaned lead example artifacts."
