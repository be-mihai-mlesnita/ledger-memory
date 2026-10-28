#!/bin/bash
# Public interactive launcher, compatible with macOS Bash 3.2.
set -euo pipefail
umask 077
repo=be-mihai-mlesnita/ledger-memory
temp=
cleanup() { if [ -n "$temp" ]; then rm -rf -- "$temp"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
fail() { printf '%s\n' "$*" >&2; exit 1; }
confirm() {
  local reply
  printf '%s [yes/ok to continue; anything else cancels]: ' "$1" >/dev/tty
  IFS= read -r reply </dev/tty || fail 'Cancelled.'
  case "$reply" in [yY][eE][sS]|[oO][kK]) ;; *) fail 'Cancelled.' ;; esac
}
[ "$(uname -s)" = Darwin ] || fail 'This command supports macOS only.'
case "$(uname -m)" in arm64|x86_64) ;; *) fail 'Supported Mac architectures: arm64 and x86_64.' ;; esac
[ "$(id -u)" -ne 0 ] || fail 'Run as your normal user, not root or sudo.'
( test -t 0 && test -t 1 ) </dev/tty >/dev/tty 2>/dev/null || fail 'Run in Terminal; a real /dev/tty is required for consent and the manager menu.'
temp=$(mktemp -d "${TMPDIR:-/tmp}/ledger-public.XXXXXXXX") || fail 'Could not create private temporary directory.'
printf '%s\n' 'Homebrew-first setup prepares Python 3.13 and uv before opening the Ledger manager, even for uninstall.' \
  'Approved prerequisite changes may remain if manifest validation fails or Ledger is later cancelled/uninstalled.' \
  'This launcher changes PATH only for this process and its children, not shell profiles or system Python.'

download() {
  if command -v curl >/dev/null 2>&1; then
    curl --disable --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
      --tlsv1.2 --connect-timeout 30 --max-time 600 --output "$2" "$1" \
      || fail 'Download failed or was incomplete; downloaded content will not be executed.'
  elif command -v wget >/dev/null 2>&1; then
    wget --no-config --no-netrc --https-only --timeout=60 --tries=1 --output-document="$2" "$1" \
      || fail 'Download failed or was incomplete; downloaded content will not be executed.'
  else
    fail 'curl or wget is required. Obtain one through your approved software channel.'
  fi
  [ -s "$2" ] || fail 'Empty download; nothing downloaded will be executed.'
}

find_brew() {
  brew=$(command -v brew || true)
  if [ ! -f "$brew" ] || [ ! -x "$brew" ]; then
    brew=
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -f "$candidate" ] && [ -x "$candidate" ]; then brew=$candidate; break; fi
    done
  fi
}
find_brew
if [ -z "$brew" ]; then
  printf '%s\n' 'Homebrew is missing. Its official installer may install Command Line Tools and request an administrator password directly in Terminal.' \
    'Follow your corporate software/security policy; this launcher does not bypass OS controls or handle passwords.' \
    'Installer: https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh' \
    'This official HTTPS HEAD URL is mutable, not checksum-pinned.'
  confirm 'Download and run the official Homebrew installer interactively?'
  download 'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh' "$temp/homebrew.sh"
  /bin/bash -n "$temp/homebrew.sh" || fail 'Homebrew installer failed Bash syntax validation; it will not run.'
  ( unset NONINTERACTIVE CI; export INTERACTIVE=1; /bin/bash "$temp/homebrew.sh" </dev/tty >/dev/tty 2>/dev/tty ) \
    || fail 'Homebrew installation failed or was cancelled. Approved prerequisite changes may remain.'
  find_brew
  [ -n "$brew" ] || fail 'Homebrew is still unavailable after installation. No shell profile was changed by this launcher.'
fi
prefix=$("$brew" --prefix) || fail 'Could not read Homebrew prefix.'
# Treat prefix output as a path, never shell code or a PATH list.
prefix_pattern='^/[A-Za-z0-9._/ -]+$'
[[ "$prefix" =~ $prefix_pattern ]] || fail 'Unsafe Homebrew prefix.'
case "/${prefix#/}/" in *'/../'*|*'/./'*|*'//'*) fail 'Unsafe Homebrew prefix.' ;; esac
[ -d "$prefix" ] || fail 'Homebrew prefix is not a directory.'
export PATH="$prefix/bin:$prefix/sbin:$prefix/opt/python@3.13/bin:$PATH"
hash -r
find_python() {
  python=
  for candidate in python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1; then
      python=$(command -v "$candidate")
      break
    fi
  done
}
check_uv() {
  local version
  command -v uv >/dev/null 2>&1 || return 1
  version=$(uv --version 2>/dev/null) || return 1
  [[ "$version" =~ ^uv\ [0-9]+\.[0-9]+\.[0-9]+ ]] || return 1
  uv sync --help >/dev/null 2>&1
}
find_python
missing=()
if [ -z "$python" ]; then missing+=(python@3.13); fi
if ! check_uv; then missing+=(uv); fi
if [ "${#missing[@]}" -ne 0 ]; then
  printf 'Command: HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ANALYTICS=1 '
  printf '%q ' "$brew" install "${missing[@]}"
  printf '\n'
  confirm 'Install only these missing prerequisites and their Homebrew dependencies?'
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ANALYTICS=1 \
    "$brew" install "${missing[@]}" </dev/tty >/dev/tty 2>/dev/tty \
    || fail 'Homebrew formula installation failed. Approved prerequisite changes may remain.'
  hash -r
fi
find_python
[ -n "$python" ] || fail 'Working Python 3.13 is still unavailable after prerequisite setup.'
check_uv || fail 'Working uv is still unavailable after prerequisite setup.'

download "https://raw.githubusercontent.com/$repo/main/deployment/distribution.json" "$temp/manifest.json"
# Bounded JSON, strict types, duplicate rejection; only validated scalar lines enter Bash.
"$python" -I -B - "$temp/manifest.json" >"$temp/manifest.fields" <<'PY' || fail 'Invalid distribution manifest; no kit will run.'
import json, re, sys
def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result
try:
    with open(sys.argv[1], "rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("Oversized manifest")
    m = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    fields = {"schema_version", "ready", "repository", "platform", "architectures",
              "version", "tag", "asset", "sha256", "kit_commit", "layout"}
    if not isinstance(m, dict) or not fields <= set(m) or set(m) - fields - {"reason"}:
        raise ValueError("Unexpected manifest fields")
    if "reason" in m and not isinstance(m["reason"], str):
        raise ValueError("Invalid reason")
    if (type(m["schema_version"]) is not int or m["schema_version"] != 1
        or m["ready"] is not True or m["repository"] != "be-mihai-mlesnita/ledger-memory"
        or m["platform"] != "darwin" or m["architectures"] != ["arm64", "x86_64"]
        or m["layout"] not in ("root", "deployment")):
        raise ValueError("Unsupported or disabled distribution")
    patterns = {"version": r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?",
                "tag": r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                "asset": r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.zip",
                "sha256": r"[0-9a-f]{64}", "kit_commit": r"[0-9a-f]{40}"}
    for key, pattern in patterns.items():
        if not isinstance(m[key], str) or not re.fullmatch(pattern, m[key]):
            raise ValueError("Invalid " + key)
    for key in ("version", "tag", "asset", "sha256", "kit_commit", "layout"):
        print(m[key])
except (ValueError, TypeError, OSError, RecursionError) as exc:
    print("Manifest validation failed: " + str(exc), file=sys.stderr)
    sys.exit(1)
PY
{
  IFS= read -r version
  IFS= read -r tag
  IFS= read -r asset
  IFS= read -r digest
  IFS= read -r kit_commit
  IFS= read -r layout
} <"$temp/manifest.fields"
printf 'Selected kit %s (%s), verifier source %s.\n' "$version" "$tag" "$kit_commit"
download "https://raw.githubusercontent.com/$repo/$kit_commit/deployment/bootstrap_verify.py" "$temp/bootstrap_verify.py"
download "https://github.com/$repo/releases/download/$tag/$asset" "$temp/kit.zip"
"$python" -I -B -c 'import hashlib, sys; f = open(sys.argv[1], "rb"); raise SystemExit(0 if hashlib.file_digest(f, "sha256").hexdigest() == sys.argv[2] else 1)' \
  "$temp/kit.zip" "$digest" || fail 'Kit SHA-256 mismatch; nothing will be extracted or executed.'
"$python" -I -B "$temp/bootstrap_verify.py" "$temp/kit.zip" "$temp/extracted" "$layout" "$digest" \
  || fail 'Unsafe or invalid kit; manager will not run.'
manager="$temp/extracted/manage.py"
if [ "$layout" = deployment ]; then manager="$temp/extracted/deployment/manage.py"; fi
# The trusted main manifest pins bytes/source; the release tag is only a locator.
# Keep this shell alive for cleanup. The manager owns all Ledger action consent.
"$python" -I -B "$manager" </dev/tty >/dev/tty 2>/dev/tty
