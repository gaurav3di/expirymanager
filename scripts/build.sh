#!/usr/bin/env bash
# Build the frontend into frontend/dist, which the backend serves from the same origin.
#
#   scripts/build.sh
#
# Run this once before the first start, and again after any frontend change. The backend serves
# whatever is in frontend/dist at the time of the request, so a rebuild does not need a restart.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend="$repo_root/frontend"

if ! command -v npm >/dev/null 2>&1; then
    echo "npm is not installed. Install Node 22.12 or newer." >&2
    exit 1
fi

cd "$frontend"

if [ ! -d node_modules ]; then
    npm install
fi

# tsc -b runs first inside this script, so a type error stops the build rather than shipping a
# bundle that compiled around it.
npm run build

echo
echo "Built into frontend/dist. Start the app with scripts/run.sh"
