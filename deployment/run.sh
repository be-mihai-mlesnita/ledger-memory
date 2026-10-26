#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

works() {
    "$1" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1
}

if [ -n "${BEMIND_DEPLOY_PYTHON:-}" ]; then
    if ! works "$BEMIND_DEPLOY_PYTHON"; then
        printf '%s\n' 'BEMIND_DEPLOY_PYTHON must point to a working Python 3.11+.' >&2
        exit 1
    fi
    exec "$BEMIND_DEPLOY_PYTHON" -I -B "$script_dir/bemind_deploy.py" "$@"
fi

if command -v uv >/dev/null 2>&1; then
    python=$(uv --no-config python find --no-project --system --no-python-downloads --offline '>=3.11' 2>/dev/null || true)
    if [ -n "$python" ] && works "$python"; then
        exec "$python" -I -B "$script_dir/bemind_deploy.py" "$@"
    fi
fi

for python in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$python" >/dev/null 2>&1 && works "$python"; then
        exec "$python" -I -B "$script_dir/bemind_deploy.py" "$@"
    fi
done
printf '%s\n' 'Python 3.11+ is required. With uv installed, run: uv python install 3.13' >&2
printf '%s\n' 'No Python, dependencies, configuration or memory were installed by this launcher.' >&2
exit 1
