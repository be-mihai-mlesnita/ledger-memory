#!/bin/bash
# Thin interactive entry point; bootstrap.sh retains release validation.
set -euo pipefail
umask 077
export GH_HOST=github.com GH_PROMPT_DISABLED=1
unset GH_DEBUG
fail() { printf '%s\n' "$*" >&2; exit 1; }
confirm() {
  local reply
  printf '%s [yes/ok to continue; anything else cancels]: ' "$1" >/dev/tty
  IFS= read -r reply </dev/tty || fail 'Cancelled.'
  case "$reply" in [yY][eE][sS]|[oO][kK]) ;; *) fail 'Cancelled.' ;; esac
}
[ "$(uname -s)" = Darwin ] || fail 'This command supports macOS only.'
case "$(uname -m)" in arm64|x86_64) ;; *) fail 'Supported Mac architectures: arm64 and x86_64.' ;; esac
( : </dev/tty ) 2>/dev/null || fail 'Run this command in Terminal; /dev/tty is required for consent and the manager menu.'
if ! command -v gh >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || fail 'Obtain GitHub CLI (gh) through your approved software channel; no approved Homebrew was found.'
  confirm 'Use your existing approved Homebrew to download/install GitHub CLI and its dependencies with brew install gh?'
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install gh || fail 'GitHub CLI installation failed.'
  command -v gh >/dev/null 2>&1 || fail 'gh is not on PATH after installation. No shell profile was changed.'
fi
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  confirm 'Browser login alone does not authenticate Terminal. Start interactive gh auth login --hostname github.com --web and save GitHub CLI authentication?'
  ( unset GH_PROMPT_DISABLED; gh auth login --hostname github.com --web </dev/tty >/dev/tty 2>/dev/tty ) || fail 'GitHub CLI login failed or was cancelled.'
  gh auth status --hostname github.com >/dev/null 2>&1 || fail 'GitHub CLI authentication is still unavailable.'
fi
commit=$(gh api --hostname github.com --method GET repos/be-mihai-mlesnita/ledger-memory/commits/main --jq .sha) || fail 'Cannot read this private repository; check your account access.'
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail 'The repository returned an invalid full source commit.'
d=$(mktemp -d "${TMPDIR:-/tmp}/ledger-homepage.XXXXXXXX")
trap 'rm -rf -- "$d"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
gh api --hostname github.com --method GET "repos/be-mihai-mlesnita/ledger-memory/contents/bootstrap.sh?ref=$commit" -H 'Accept: application/vnd.github.raw+json' >"$d/bootstrap.sh" || fail 'Bootstrap download failed; nothing downloaded will be executed.'
test -s "$d/bootstrap.sh" || fail 'Empty bootstrap download; nothing will be executed.'
/bin/bash "$d/bootstrap.sh" --source-commit "$commit" </dev/tty
