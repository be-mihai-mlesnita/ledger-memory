# Ledger Memory For Mac

Install, upgrade or uninstall BeMind's shared memory with one pasted command.
This repository is public. **No GitHub account, access token or GitHub CLI is needed.**

**Public curl/wget setup:** the downloaded launcher checks prerequisites and asks
before installing anything. No manual Homebrew, Python or uv setup is required first.

## Start Here

1. Have BeMind installed. This bootstrap does not install BeMind itself.
2. Open **Terminal**, paste one command below and press Return. Do not add `sudo`.
3. If Homebrew is missing, setup offers its official installer from `brew.sh`.
   It explains the changes and may request Command Line Tools or your Mac's
   administrator password directly in Terminal. Follow your corporate software policy.
4. Setup checks for working **Python 3.13** and **uv**. It shows a `brew install`
   command containing only missing prerequisites and waits for `yes` or `ok`.
5. The verified Ledger manager then offers install, upgrade or uninstall as
   appropriate, with a separate confirmation for the exact operation.
6. After installation or upgrade, restart BeMind yourself when your work can
   safely stop. Running instances do not automatically load the new configuration.

Enter or anything other than `yes`/`ok` cancels an approval prompt. The manager
also provides **Cancel**. This bootstrap prepares its tools before displaying
the manager, including when you intend to uninstall. Previously approved Homebrew
or package changes remain if a later step fails or is cancelled.

## One Command

**With curl**, included with macOS:

```bash
/bin/bash -c 's=$(curl --disable -fsSL --proto =https --proto-redir =https https://raw.githubusercontent.com/be-mihai-mlesnita/ledger-memory/main/install.sh) && test -n "$s" && /bin/bash -c "$s"'
```

**Or with wget**, if it is already installed:

```bash
/bin/bash -c 's=$(wget --no-config --no-netrc --https-only -qO- https://raw.githubusercontent.com/be-mihai-mlesnita/ledger-memory/main/install.sh) && test -n "$s" && /bin/bash -c "$s"'
```

Both commands download the complete entry script before executing it. A failed
or empty download is not executed. No username, password, browser cookies or token
are put into these commands.

**For future upgrades:** run the same command and choose **Upgrade**.
**To uninstall:** run it and choose **Uninstall**. Uninstall retains memory,
histories, backups, receipts and the separately installed prerequisite tools.

## What Setup Does

- Finds Homebrew on PATH or in `/opt/homebrew/bin` and `/usr/local/bin`.
- If needed and approved, downloads the Homebrew installer linked by `brew.sh`:
  `https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh`.
- Uses Homebrew to install missing `python@3.13` and `uv`. Existing compatible
  tools are reused. Python 3.13 is installed alongside, not in place of, system Python.
- Updates PATH only for this process and its children; it does not edit shell profiles.
  Homebrew's own approved installer may make the system changes it describes.
- Downloads the public distribution manifest and the exact selected release ZIP.
  The archive SHA-256 is checked before extraction or execution.
- Retrieves the archive verifier from the full source commit pinned by the manifest.
  The verifier rejects unsafe paths, links, unexpected files and invalid metadata.
- Starts the existing receipt-owned manager. It preserves unrelated configuration
  and refuses unproved ownership rather than deleting or adopting arbitrary files.

Automatic indexing threads use one-third of usable logical CPUs, rounded down
and bounded to 1..32. A 12-CPU Mac gets four threads. Upgrades preserve the saved
choice. This is an inference-thread setting, not a strict CPU-percentage quota.
An already-running model worker keeps its previous setting until safely restarted.

Model downloads remain opt-in. Conversation histories are not imported or uploaded
by this installer. No running BeMind instance or indexing job is force-stopped.

## If Setup Stops

| Situation | What to do |
| --- | --- |
| Homebrew was just installed but Terminal cannot find it | The new launcher checks its standard locations. For manual commands on Apple Silicon, run `eval "$(/opt/homebrew/bin/brew shellenv)"`; for a standard Intel installation use `/usr/local/bin/brew`. |
| Homebrew asks for an administrator password | Enter it only into Homebrew's Terminal prompt if permitted by your organization. Do not run this bootstrap as root or bypass corporate controls. |
| Python 3.13 or uv is still unavailable | Read the Homebrew error, resolve it through your approved software process, and rerun the same command. No Ledger operation is performed after a failed prerequisite check. |
| Network, certificate or checksum error | Stop and resolve the download problem. Do not disable TLS verification or bypass checksum checks. |
| Unmanaged installation, edited configuration or recovery marker | Preserve the files and reconcile the reported state. Do not delete memory or receipts to force installation. |
| An active session prevents an operation | Let it finish and close relevant sessions when safe, then retry. Do not force-delete runtimes or lock files. |

## Verification And Limits

The public entry script and manifest are obtained from the project's `main` branch
over HTTPS. The manifest pins the release bytes and verifier source commit; the
release tag is a download locator, not an independent cryptographic signature.
No GitHub REST API or authentication is required by the public launcher.

Homebrew's official `HEAD` installer is mutable and is not checksum-pinned by this
bootstrap. Its complete download and Bash syntax are checked before execution,
and the user must approve it first. Prerequisite setup can happen before later
manifest or release validation; those approved tool changes are not rolled back.

Missing-tool installation and cancellation are tested with inert, isolated fixtures.
Tests cover curl/wget, Apple Silicon/Intel discovery, consent, malformed manifests,
partial downloads, archive verification and cleanup. A real Homebrew installation
and native Intel-Mac execution are not part of that fixture validation.

`bootstrap.sh` remains available for the older GitHub-CLI-based workflow. The new
public entry does not use it. Manual package launchers and advanced options are
documented in `deployment/README.md`.

## Source Layout

| Path | Purpose |
| --- | --- |
| `install.sh` | Public Homebrew-first curl/wget entry |
| `deployment/` | Receipt-owned install, upgrade and uninstall engine, gateway and tests |
| `deployment/distribution.json` | Selected release, digest and source pin |
| `deployment/core-distribution.json` | Reproducible runtime-source packaging recipe |
| `upstream/ledger-memory/` | Separately identified core reference snapshot |
| `upstream/RELEASE.json` | Core reference and runtime provenance |
| `repo_tools/` | Reviewed source selection and publication auditing |

The reference snapshot is `0.3.0-rc3`; the distributed runtime is based on
`0.3.0rc6`, profile `bemind-runtime-1`. That profile contains one documented
diagnostic-message wording change; control and data logic are unchanged.
Original input archives and local operational evidence remain outside GitHub.

The currently selected kit remains `bemind-mac-setup-1`, SHA-256
`601c2dc589c720b8510161bf35cf5cf65c3d0ab42cbd3ccc6e6b662fa989e3a5`.
Changing the public launcher does not change that ZIP or upgrade a user's existing
installation without confirmation.
