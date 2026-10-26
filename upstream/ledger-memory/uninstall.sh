#!/usr/bin/env bash
# Detach ledger-memory from opencode and remove the program. Your MEMORY is never touched:
# it stays in $LEDGER_HOME (default ~/.ledger-memory), with its backups, until you remove it yourself.
set -euo pipefail
APP="${LEDGER_APP_DIR:-$HOME/.local/share/ledger-memory}"
[ -x "$APP/.venv/bin/python" ] && "$APP/.venv/bin/python" "$APP/install/configure_opencode.py" --app "$APP" --remove || true
LINK="$HOME/.local/bin/ledger"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$APP/.venv/bin/ledger" ]; then rm "$LINK"; echo "removed $LINK"; fi
if [ -e "$APP" ]; then
  OLD="$APP.removed-$(date -u +%Y%m%d%H%M%S)"; mv "$APP" "$OLD"
  echo "program moved to $OLD (delete it when you like). Memory left in ${LEDGER_HOME:-$HOME/.ledger-memory}."
fi
if [ -d "$APP-versions" ]; then
  echo "installed versions remain in $APP-versions (delete the folder when you like)."
fi
