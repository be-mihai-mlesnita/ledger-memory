#!/bin/bash
# Run with /bin/bash, including on macOS Bash 3.2.
set -euo pipefail
umask 077
export GH_HOST=github.com GH_PROMPT_DISABLED=1 UV_NO_MODIFY_PATH=1
unset GH_DEBUG
repo=be-mihai-mlesnita/ledger-memory
temp=
cleanup() { if [ -n "$temp" ]; then rm -rf -- "$temp"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
fail() { printf '%s\n' "$*" >&2; exit 1; }
confirm() {
    local reply
    if ! ( : </dev/tty ) 2>/dev/null; then
        fail 'A controlling terminal is required for prerequisite consent. Nothing was installed.'
    fi
    printf '%s [yes/ok to continue; anything else cancels]: ' "$1" >/dev/tty
    IFS= read -r reply </dev/tty || fail 'Cancelled. Nothing further was installed.'
    case "$reply" in [yY][eE][sS]|[oO][kK]) ;; *) fail 'Cancelled. Nothing further was installed.' ;; esac
}

[ "$#" -eq 2 ] && [ "$1" = --source-commit ] || fail 'Usage: /bin/bash bootstrap.sh --source-commit REVIEWED_FULL_40_HEX_COMMIT'
source_commit=$2
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || fail 'A full 40-character lowercase source commit resolved from the authenticated repository is required.'
[ "$(uname -s)" = Darwin ] || fail 'This bootstrap supports macOS only.'
arch=$(uname -m)
case "$arch" in arm64|x86_64) ;; *) fail 'Supported Mac architectures: arm64 and x86_64.' ;; esac
command -v gh >/dev/null 2>&1 || fail 'Install the GitHub CLI (gh) through your approved software channel, then run gh auth login --hostname github.com. Browser login alone does not authenticate the terminal.'
gh auth status --hostname github.com >/dev/null 2>&1 || fail 'GitHub CLI authentication failed. Run gh auth login --hostname github.com with repository access, then retry. Browser login alone is insufficient.'
temp=$(mktemp -d "${TMPDIR:-/tmp}/ledger-bootstrap.XXXXXXXX") || fail 'Could not create private temporary directory.'

# gh supplies its own jq engine, so disabled/invalid releases stop before Python setup.
# Only validated scalar lines cross into the shell; never source/eval remote data.
manifest_filter='
  def text($re): type == "string" and test("\\A(?:" + $re + ")\\z");
  if .schema_version != 1 or .repository != "be-mihai-mlesnita/ledger-memory" then
    error("Invalid distribution schema or repository")
  elif .ready == false then "disabled"
  elif .ready != true or .platform != "darwin"
    or .architectures != ["arm64", "x86_64"]
    or (.version | text("[0-9]+[.][0-9]+[.][0-9]+(-[A-Za-z0-9.-]+)?") | not)
    or (.tag | text("[A-Za-z0-9][A-Za-z0-9._-]{0,127}") | not)
    or (.asset | text("[A-Za-z0-9][A-Za-z0-9._-]{0,159}[.]zip") | not)
    or (.sha256 | text("[0-9a-f]{64}") | not)
    or (.kit_commit | text("[0-9a-f]{40}") | not)
    or (.layout != "root" and .layout != "deployment") then
    error("Invalid enabled distribution manifest")
  else "ready", .version, .tag, .asset, .sha256, .kit_commit, .layout end'
gh api --hostname github.com --method GET \
    "repos/$repo/contents/deployment/distribution.json?ref=$source_commit" \
    -H 'Accept: application/vnd.github.raw+json' --jq "$manifest_filter" >"$temp/manifest.fields" \
    || fail 'Could not retrieve/validate the pinned distribution manifest. Check repository access; no kit was downloaded.'
{
    IFS= read -r status || fail 'Empty distribution manifest.'
    [ "$status" != disabled ] || fail 'Distribution is disabled/not ready. An approved kit tag, commit and SHA-256 must be reviewed before download or installation. No Python/uv or kit was installed by this bootstrap.'
    [ "$status" = ready ] || fail 'Invalid distribution status.'
    IFS= read -r version
    IFS= read -r tag
    IFS= read -r asset
    IFS= read -r digest
    IFS= read -r kit_commit
    IFS= read -r layout
} <"$temp/manifest.fields"

resolved=$(gh api --hostname github.com --method GET "repos/$repo/commits/$tag" --jq '.sha') \
    || fail 'Could not resolve the exact release tag.'
[ "$resolved" = "$kit_commit" ] || fail 'Release tag provenance does not match the reviewed kit commit.'
gh api --hostname github.com --method GET "repos/$repo/releases/tags/$tag" \
    --jq 'if .draft == false then .tag_name, (.assets[] | .name) else error("Draft release refused") end' \
    >"$temp/release.fields" || fail 'Could not verify the exact published release.'
found=false
{
    IFS= read -r release_tag
    [ "$release_tag" = "$tag" ] || fail 'Release tag mismatch.'
    while IFS= read -r name; do
        if [ "$name" = "$asset" ]; then
            [ "$found" = false ] || fail 'Duplicate release asset.'
            found=true
        fi
    done
} <"$temp/release.fields"
[ "$found" = true ] || fail 'The reviewed kit asset is absent from the exact release.'

python=
check_python() { "$1" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
if [ -n "${BEMIND_DEPLOY_PYTHON:-}" ]; then
    check_python "$BEMIND_DEPLOY_PYTHON" || fail 'BEMIND_DEPLOY_PYTHON must name an existing working Python >=3.11.'
    python=$BEMIND_DEPLOY_PYTHON
fi
for candidate in python3.13 python3.14 python3.12 python3.11 python3 python; do
    [ -z "$python" ] || break
    if command -v "$candidate" >/dev/null 2>&1 && check_python "$candidate"; then
        python=$(command -v "$candidate")
        break
    fi
done
if [ -z "$python" ]; then
    if ! command -v uv >/dev/null 2>&1; then
        command -v brew >/dev/null 2>&1 || fail 'Python >=3.11 is required. Ask your approved software channel for Python, or uv (or approved Homebrew). No runtime or package-manager installer was downloaded.'
        confirm 'No suitable Python or uv found. Use your existing approved Homebrew to run brew install uv? This downloads and installs uv and any dependencies.'
        HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install uv \
            || fail 'Homebrew could not install uv. Resolve this through your approved software channel.'
        command -v uv >/dev/null 2>&1 || fail 'uv is not on PATH after Homebrew installation. No shell profile was changed.'
    fi
    python=$(UV_PYTHON_DOWNLOADS=never uv python find --no-project '>=3.11' 2>/dev/null) || python=
    if [ -z "$python" ] || ! check_python "$python"; then
        confirm 'No suitable Python found. Allow existing uv to download/install managed Python 3.13 from its configured distribution source? No shell profile will be changed.'
        UV_PYTHON_DOWNLOADS=manual uv python install 3.13 --no-bin \
            || fail 'uv could not install Python 3.13.'
        python=$(UV_PYTHON_DOWNLOADS=never uv python find --no-project 3.13) \
            || fail 'uv could not locate Python 3.13.'
        check_python "$python" || fail 'Selected Python is not >=3.11.'
    fi
fi

gh api --hostname github.com --method GET \
    "repos/$repo/contents/deployment/bootstrap_verify.py?ref=$source_commit" \
    -H 'Accept: application/vnd.github.raw+json' >"$temp/bootstrap_verify.py" \
    || fail 'Pinned verifier download failed; it will not be executed.'
[ -s "$temp/bootstrap_verify.py" ] || fail 'Empty pinned verifier download.'
printf 'Selected reviewed kit %s (%s), source %s.\n' "$version" "$tag" "$kit_commit"
gh release download "$tag" --repo "github.com/$repo" --pattern "$asset" --output "$temp/kit.zip" \
    || fail 'Kit download failed or was incomplete; nothing will be extracted or executed.'
[ -s "$temp/kit.zip" ] || fail 'Empty kit download.'
actual=$(shasum -a 256 "$temp/kit.zip") || fail 'Could not calculate kit SHA-256.'
[ "${actual%% *}" = "$digest" ] || fail 'Kit SHA-256 mismatch; nothing will be extracted or executed.'
"$python" -I -B "$temp/bootstrap_verify.py" "$temp/kit.zip" "$temp/extracted" "$layout" "$digest" \
    || fail 'Unsafe or invalid kit; manager will not run.'
manager="$temp/extracted/manage.py"
if [ "$layout" = deployment ]; then manager="$temp/extracted/deployment/manage.py"; fi
# Keep this shell alive so its EXIT trap removes only its own temporary directory.
# manage.py owns the action/menu/confirmation and must not retain these temp paths.
"$python" -I -B "$manager"
