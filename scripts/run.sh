#!/usr/bin/env bash
# ExpiryManager: start, develop, or test.
#
#   scripts/run.sh              start the app on http://127.0.0.1:8000
#   scripts/run.sh dev          the same, with reload, for backend work
#   scripts/run.sh web          the Vite dev server on http://127.0.0.1:5173
#   scripts/run.sh test         the backend suite, then the frontend suite
#
# Anything after the mode is handed to the underlying command, so
#   scripts/run.sh --data-dir /tmp/em-scratch --log-level debug
# works, and so does
#   scripts/run.sh test -k governor
#
# The host, the port and the /fyers/callback path are fixed by the redirect URI registered with
# Fyers, which Fyers matches character for character. They are deliberately not parameterised
# here. The scheme is http for the same reason, and --https is the opt in for a re-registered
# https URI.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$repo_root/backend"
frontend="$repo_root/frontend"

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "$1 is not installed. $2" >&2
        exit 1
    fi
}

warn_if_frontend_is_not_built() {
    if [ ! -f "$frontend/dist/index.html" ]; then
        echo "Note: frontend/dist is missing, so the app will serve the API without a UI."
        echo "      Run scripts/build.sh once, then start again."
        echo
    fi
}

mode="start"
if [ $# -gt 0 ]; then
    case "$1" in
        start | dev | web | test)
            mode="$1"
            shift
            ;;
    esac
fi

case "$mode" in
    start)
        need uv "See https://docs.astral.sh/uv/ for installation."
        warn_if_frontend_is_not_built
        cd "$backend"
        uv sync --quiet
        exec uv run expirymanager "$@"
        ;;

    dev)
        need uv "See https://docs.astral.sh/uv/ for installation."
        echo "Backend with reload. For the UI, run scripts/run.sh web in a second terminal."
        echo
        cd "$backend"
        uv sync --quiet
        exec uv run expirymanager --reload "$@"
        ;;

    web)
        need npm "Install Node 22.12 or newer."
        cd "$frontend"
        if [ ! -d node_modules ]; then
            npm install
        fi
        # 127.0.0.1 and not localhost: the session cookie the OAuth callback sets on port 8000 is
        # only sent from port 5173 under the same host. The dev server proxies /api to the
        # backend, so the browser sees one origin here as it does in production.
        echo "Open http://127.0.0.1:5173, not localhost, and start the backend too."
        echo
        exec npm run dev -- "$@"
        ;;

    test)
        need uv "See https://docs.astral.sh/uv/ for installation."
        need npm "Install Node 22.12 or newer."
        cd "$backend"
        uv sync --quiet
        uv run pytest tests/ -q "$@"
        cd "$frontend"
        if [ ! -d node_modules ]; then
            npm install
        fi
        npm run typecheck
        npm test
        ;;
esac
