#!/usr/bin/env sh
# Install ledger-memory for the current user and wire it into opencode. Safe to re-run (that is how you upgrade).
#
# Never deletes anything. Each version is built and self-tested in its own folder under <app>-versions/; only when it
# passes does <app> (a symlink) switch to it, so a failed download or self-test leaves the working version in place.
# An existing memory is backed up before anything changes; the memory itself ($LEDGER_HOME, default ~/.ledger-memory)
# is never moved or rewritten by this script.
#
#   ./install.sh                 install + configure opencode
#   ./install.sh --no-opencode   install only (wire another MCP client yourself; see README)
#
# Overrides: LEDGER_APP_DIR (default ~/.local/share/ledger-memory), LEDGER_HOME (default ~/.ledger-memory).
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="${LEDGER_APP_DIR:-$HOME/.local/share/ledger-memory}"
STORE="${LEDGER_HOME:-$HOME/.ledger-memory}"
OPENCODE=1
for arg in "$@"; do
  case "$arg" in
    --no-opencode) OPENCODE=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
step() { printf '\n==> %s\n' "$*"; }
stamp() { date -u +%Y%m%d%H%M%S; }

step "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it, then re-run this script:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh      (or: brew install uv)" >&2
  exit 3
fi
uv --version

# Read version from pyproject.toml
VERSION=""
if [ -f "$SRC/pyproject.toml" ]; then
  VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
fi
[ -n "$VERSION" ] || VERSION="unknown"

VERSIONS="$APP-versions"
mkdir -p "$VERSIONS"
# A fresh, unique folder per run: never reuse (and so never overwrite) an existing version, even within one second.
NEW="$(mktemp -d "$VERSIONS/$VERSION-$(stamp)-XXXXXX")"

step "Preparing the new version at $NEW"
(cd "$SRC" && tar --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude='*.pyc' -cf - .) | (cd "$NEW" && tar -xf -)

step "Installing the exact pinned dependencies, including the optional local search runtime (uv sync --frozen --extra semantic; fetches Python 3.11+ if needed). No model is downloaded"
if ! (cd "$NEW" && uv sync --frozen --extra semantic --quiet); then
  echo "install failed; your current version is unchanged" >&2
  echo "Failed version kept at $NEW for inspection" >&2
  exit 1
fi

LEDGER_NEW="$NEW/.venv/bin/ledger"

step "Self-test (on a throwaway store; your memory is not touched)"
if ! "$LEDGER_NEW" selftest; then
  echo "install failed; your current version is unchanged" >&2
  echo "Failed version kept at $NEW for inspection" >&2
  exit 1
fi

BACKED_UP=0
CURRENT_LEDGER=""
if [ -L "$APP" ] && [ -x "$APP/.venv/bin/ledger" ]; then
  CURRENT_LEDGER="$APP/.venv/bin/ledger"
elif [ -d "$APP" ] && [ -x "$APP/.venv/bin/ledger" ]; then
  CURRENT_LEDGER="$APP/.venv/bin/ledger"
fi

if [ -f "$STORE/memory.db" ] && [ -n "$CURRENT_LEDGER" ]; then
  step "Backing up your existing memory before upgrading"
  LEDGER_HOME="$STORE" "$CURRENT_LEDGER" backup
  BACKED_UP=1
fi

step "Switching to the new version"
SWITCH_TMP="$APP.switch.$$"
LEGACY=""
replace_link() {   # atomically point the symlink $APP at $NEW (rename over the old link, never follow it)
  ln -sfn "$NEW" "$SWITCH_TMP" || return 1
  if [ "$(uname -s)" = "Darwin" ]; then mv -fh "$SWITCH_TMP" "$APP"; else mv -fT "$SWITCH_TMP" "$APP"; fi
}
if [ -L "$APP" ]; then
  PREVIOUS="$(readlink "$APP")"
  replace_link || { rm -f "$SWITCH_TMP"; echo "switch failed; still on $PREVIOUS" >&2; exit 1; }
elif [ -d "$APP" ]; then
  LEGACY="$VERSIONS/legacy-$(stamp)"
  mv "$APP" "$LEGACY"
  echo "previous directory install moved to $LEGACY (kept as an archive; its programs pointed at the old path)"
  ln -sfn "$NEW" "$APP" || { mv "$LEGACY" "$APP"; echo "switch failed; previous install restored" >&2; exit 1; }
else
  ln -sfn "$NEW" "$APP"
fi

if [ ! -L "$APP" ] || [ "$(readlink "$APP")" != "$NEW" ]; then
  echo "switch failed; restoring previous state" >&2
  if [ -n "$LEGACY" ] && [ -d "$LEGACY" ] && [ ! -e "$APP" ]; then
    mv "$LEGACY" "$APP"
  fi
  exit 1
fi
echo "now using $NEW (earlier versions stay in $VERSIONS; delete old ones by hand once you are happy)"

step "Creating your memory at $STORE (an existing one is kept as is)"
LEDGER_HOME="$STORE" "$APP/.venv/bin/ledger" init

if [ -f "$STORE/memory.db" ] && [ "$BACKED_UP" = 0 ]; then
  step "Backing up your existing memory"
  LEDGER_HOME="$STORE" "$APP/.venv/bin/ledger" backup
fi

step "Putting the 'ledger' command on your PATH"
mkdir -p "$HOME/.local/bin"
LINK="$HOME/.local/bin/ledger"
if [ -e "$LINK" ] && [ "$(readlink "$LINK" 2>/dev/null || true)" != "$APP/.venv/bin/ledger" ]; then
  echo "left alone: $LINK already exists and is not ours. Use $APP/.venv/bin/ledger directly."
else
  ln -sf "$APP/.venv/bin/ledger" "$LINK"
  echo "linked $LINK"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: ~/.local/bin is not on your PATH; add it to use 'ledger' by name." ;; esac
fi

PENDING=0
if [ "$OPENCODE" = 1 ]; then
  step "Wiring the memory into opencode (global config)"
  "$APP/.venv/bin/python" "$APP/install/configure_opencode.py" --app "$APP" --store "$STORE" || PENDING=1
fi

step "Done"
echo "memory:   $STORE"
echo "program:  $APP"
echo "commands: ledger status | ledger recall \"...\" | ledger backup   (ledger --help for all)"
if [ "$PENDING" = 1 ]; then
  echo "ACTION NEEDED: opencode was NOT configured automatically. Merge the snippet printed above into your"
  echo "               opencode config by hand (back it up first), then restart opencode. Everything else is installed."
  exit 4
fi
[ "$OPENCODE" = 1 ] && echo "next:     restart opencode, then ask it: \"what do you remember about me?\""
exit 0
