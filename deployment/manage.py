"""Interactive macOS frontend for the receipt-owned deployment transaction engine.

Entrypoint: python -I -B deployment/manage.py [install options]. No shell or eval.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bemind_deploy as d

INSTALLER_VERSION = "0.3.1"


class InspectionError(d.DeployError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def parser():
    result = d.parser("install")
    result.description = __doc__
    result.set_defaults(app_root=None, store=None)
    result.add_argument("--action", choices=("install", "upgrade", "uninstall"),
                        help="Select an action, still requiring the full confirmation prompt.")
    result.add_argument("--inspect-json", action="store_true", help="Read-only inspection and preview; never prompt or execute.")
    result.add_argument("--keep-runtime", action="store_true", help="Uninstall registration only; retain owned runtimes too.")
    result.add_argument("--auto-model", action="store_true", help="For install/upgrade only: after final confirmation, fetch and enable pinned BGE-M3 via curl if not cached.")
    return result


def inspect_installation(args):
    args = argparse.Namespace(**vars(args))
    config = d.safe_path(Path(args.config_dir))
    args.config_dir = str(config)
    state = config / "ledger-memory-deployment"
    if d.read_bytes(state / "pending.json") is not None:
        raise InspectionError("recovery", "Interrupted deployment: preserve pending.json and backups; reconcile recovery before setup.")
    try:
        receipt = d.load_receipt(state / "receipt.json")
        saved_bemind = None
        defaults = d.parser("install")
        for key in ("app_root", "store"):
            explicit = getattr(args, key)
            selected = d.safe_path(Path(explicit or (receipt[key] if receipt else defaults.get_default(key))))
            if receipt and str(selected) != receipt[key]:
                raise d.DeployError("Explicit application/store override conflicts with the receipt; use its original paths.")
            setattr(args, key, str(selected))
        app, store = Path(args.app_root), Path(args.store)
        for directory in (config, state, app, store):
            d.safe_path(directory)
            if directory.exists() and not directory.is_dir():
                raise d.DeployError("Configuration, application and store paths must be directories.")
        if d.read_bytes(store / "restore-in-progress.json") is not None:
            raise InspectionError("recovery", "Memory restore is incomplete; preserve the store and resolve recovery first.")
        database = d.safe_path(store / "memory.db")
        if database.exists():
            info = database.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise d.DeployError("Memory database must be a single-link regular file; preserve it for recovery.")
        elif store.exists() and any(store.iterdir()):
            raise InspectionError("recovery", "Nonempty memory store has no memory.db; preserve its contents and reconcile before setup.")
        for name in ("config.json", "bemind.json", "bemind.jsonc"):
            path = config / name
            obj = d.parse_config(d.read_bytes(path))
            mcp = obj.get("mcp", {})
            if not isinstance(mcp, dict):
                raise d.DeployError("Global mcp setting is not an object.")
            for key in ("ledger", "memory"):
                if key not in mcp:
                    continue
                owned = (receipt and receipt["status"] == "installed" and str(path) == receipt["config"]
                         and key == ("memory" if receipt.get("memory_mode") == "luna" else "ledger"))
                if not owned:
                    raise InspectionError("unmanaged", "Unmanaged memory configuration found. "
                        "Automatic adoption/uninstall is refused. Review explicit migration: bemind_deploy.py install "
                        "--migrate-existing-ledger --store PATH, with explicit --replace-instruction "
                        "and --agents-file/--agents-section if needed. Manual binaries remain unowned.")
                if not d.same_entry(mcp[key], receipt["entry"]):
                    raise d.DeployError("Managed memory entry was edited; reconcile it before setup.")
        chosen, snapshots = d.config_state(config, receipt, store=store)
        if receipt:
            runtimes = d.runtime_paths(receipt, app)
            if receipt["status"] == "installed":
                if not runtimes[-1].is_dir():
                    raise d.DeployError("Installed runtime is missing; preserve the receipt for recovery.")
                components = [Path(receipt["entry"]["command"][0])]
                if receipt.get("memory_mode") == "luna":
                    components.extend(runtimes[-1] / "gateway" / name for name in
                                      ("memory_gateway.py", "ledger_backend.py", "gateway_backend_contract.py", "worker_guard.mjs"))
                if any(not path.is_file() for path in components):
                    raise d.DeployError("Installed runtime component is missing; preserve the receipt for recovery.")
                key = "memory" if receipt.get("memory_mode") == "luna" else "ledger"
                if not d.same_entry(d.parse_config(snapshots[chosen]).get("mcp", {}).get(key), receipt["entry"]):
                    raise d.DeployError("Managed memory entry is missing; reconcile it before setup.")
                for manifest in receipt.get("manifest", []):
                    d.restore_manifest(manifest, d.read_bytes(Path(manifest["path"])), receipt.get("agents"))
            rules = d.read_bytes(Path(receipt["instruction"]))
            if ((rules is None and receipt["status"] == "installed")
                    or rules is not None and d.digest(rules) != receipt["rules_hash"]):
                raise d.DeployError("Managed rules are missing or edited; reconcile them before setup.")
            for gateway in [*receipt.get("gateway_history", []), *([receipt["gateway"]] if receipt.get("gateway") else [])]:
                data = d.read_bytes(Path(gateway["settings"]))
                if data is None or d.digest(data) != gateway["settings_hash"]:
                    raise d.DeployError("Managed gateway settings are missing or edited.")
                from gateway.memory_gateway import GatewayError, settings as gateway_settings
                try:
                    validated = gateway_settings(Path(gateway["settings"]))
                except GatewayError:
                    raise d.DeployError("Invalid managed gateway settings; preserve state and reconcile before setup.") from None
                settings = d.parse_config(data)
                d.validate_thread_selection(settings)
                if (settings.get("owner") != receipt["owner"] or settings.get("store") != receipt["store"]
                        or settings.get("ledger_python") != str(d.runtime_python(Path(gateway["runtime"])))
                        or settings.get("state_dir") != gateway["state_dir"] or settings.get("model") != d.LUNA_MODEL):
                    raise d.DeployError("Managed gateway settings ownership mismatch.")
                if gateway == receipt.get("gateway") and any(settings.get(k) != receipt.get(k)
                        for k in ("embed_threads", "embed_threads_policy")):
                    raise d.DeployError("Gateway and receipt embedding thread selections differ.")
                if gateway == receipt.get("gateway"):
                    saved_bemind = validated["bemind_command"][0]
                if d.read_bytes(Path(gateway["state_dir"]) / "quarantine.json") is not None:
                    raise InspectionError("recovery", "Gateway is quarantined; preserve its state and reconcile recovery first.")
        else:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            for legacy in {app, data_home / "ledger-memory", Path.home() / ".local/share/ledger-memory"}:
                d.safe_path(legacy)
                if legacy.exists() and (not legacy.is_dir() or any(legacy.iterdir())):
                    raise InspectionError("unmanaged", "Unmanaged runtime/module directory found. No managed ownership is inferred "
                        "from a ledger command. Preserve manual binaries; review an explicit low-level migration into a separate app root.")
            if d.read_bytes(state / "MEMORY-RULES.md") is not None:
                raise InspectionError("unmanaged", "Memory rules exist without a receipt; reconcile explicit migration first.")
            if state.exists() and any(path.name != "installer.lock" for path in state.iterdir()):
                raise InspectionError("recovery", "Deployment state exists without a receipt; preserve its artifacts and backups for recovery.")
        if args.migrate_existing_ledger:
            raise InspectionError("unmanaged", "Automatic migration is not supported here. Review the explicit low-level migration command separately.")
        if d.deployment_mode(args, receipt) == "luna":
            d.luna_store_preflight(store)
        candidate = args.bemind_command or saved_bemind or d.shutil.which("bemind") or str(Path.home() / ".bemind/bin/bemind")
        args.bemind_command = str(Path(candidate).expanduser().absolute())
        return args, receipt, receipt["status"] if receipt else "fresh"
    except InspectionError:
        raise
    except (d.DeployError, ValueError, TypeError, KeyError) as exc:
        message = str(exc) if isinstance(exc, d.DeployError) else "Invalid receipt/settings; preserve state and recover from backups."
        raise InspectionError("invalid", message) from None


def material_state(args, receipt):
    """Read-only approval fingerprint, not a scan of memory contents or model caches."""
    config, app, store = map(Path, (args.config_dir, args.app_root, args.store))
    state = config / "ledger-memory-deployment"
    files = {config / name for name in ("config.json", "bemind.json", "bemind.jsonc")}
    files.update(state / name for name in ("receipt.json", "pending.json", "MEMORY-RULES.md"))
    files.add(store / "restore-in-progress.json")
    directories = {config, state, app, app / "versions"}
    # Indexing changes database bytes, WALs, and directory timestamps without
    # changing approval scope. Replacement, ownership and permissions still matter.
    identities = {store, store / "memory.db"}
    metadata = set()
    executables = set()
    if receipt:
        files.update(Path(item["path"]) for item in receipt.get("manifest", []))
        for runtime in map(Path, receipt["runtimes"]):
            directories.add(runtime)
            files.add(runtime / d.MARKER)
            files.update(runtime / name for name in ("pyproject.toml", "uv.lock"))
            if (runtime / "gateway").is_dir():
                identities.add(runtime / "gateway")
                files.update(p for p in (runtime / "gateway").iterdir() if p.suffix in (".py", ".mjs"))
            source = d.safe_path(runtime / "src")
            if source.is_dir():
                identities.add(source)
                files.update(source.rglob("*.py"))
            executables.update((d.runtime_python(runtime), runtime / ".venv/bin/ledger"))
        for gateway in [*receipt.get("gateway_history", []), *([receipt["gateway"]] if receipt.get("gateway") else [])]:
            files.add(Path(gateway["settings"]))
            private = Path(gateway["state_dir"])
            identities.update((private, private / "receipts"))
            files.update(private / name for name in ("identity.json", "quarantine.json"))
            # Active scopes, supervisor endpoints and durable write receipts are
            # checked/quiesced by the transaction engine, not frozen by the UI.
    if args.command == "install":
        executables.add(Path(args.bemind_command))
        release = d.safe_path(Path(args.archive))
        files.update((release, release.with_suffix(release.suffix + ".sha256"), d.HERE / "MEMORY-RULES.md"))
        files.update(d.HERE / name for name in ("bemind_deploy.py", "jsonc_edit.py", "manage.py", "model_download.py"))
        if (d.HERE / "gateway").is_dir():
            files.update(p for p in (d.HERE / "gateway").iterdir() if p.suffix in (".py", ".mjs"))
        uv = getattr(args, "_uv_command", None) or d.shutil.which("uv")
        if uv:
            executables.add(Path(uv))
        if getattr(args, "_brew_command", None):
            executables.add(Path(args._brew_command))
        if args.ca_bundle:
            files.add(d.safe_path(Path(args.ca_bundle)))
        if args.agents_file:
            files.add(d.safe_path(Path(args.agents_file)))
        if args.model_dir:
            model = d.safe_path(Path(args.model_dir))
            directories.add(model)
            metadata.update(model.iterdir())
    result = {}
    for path in files | directories | identities | metadata | executables:
        if path not in executables:
            d.safe_path(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[str(path)] = None
            continue
        signature = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)
        if path in identities:
            if stat.S_ISDIR(info.st_mode):
                signature = signature[:-1]
            extra = None
        elif path in directories and stat.S_ISDIR(info.st_mode):
            extra = sorted(p.name for p in path.iterdir())
        elif path in files:
            extra = d.digest(d.read_bytes(path))
        elif path in executables:
            target = path.stat()
            extra = (str(path.resolve()), target.st_dev, target.st_ino, target.st_mode,
                     target.st_uid, target.st_gid, target.st_nlink, d.digest(path.read_bytes()))
        else:
            extra = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        result[str(path)] = (signature, extra)
        for parent in path.parents:
            info = parent.stat()
            result["parent:" + str(parent)] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    result["environment"] = dict(os.environ)
    return result


def interactive_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def answer(prompt, input_fn=None):
    # Injected input is useful for tests, never a bypass of terminal-only consent.
    if not interactive_terminal():
        print("Interactive terminal required; piped input cannot authorize changes.")
        return ""
    try:
        return (input_fn or input)(prompt).strip().lower()
    except EOFError:
        return ""


def homebrew_command():
    for candidate in (d.shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).absolute())
    return None


def prerequisites(args):
    if args.command == "uninstall":
        return {"required": False, "blockers": [], "note": "Uninstall needs neither BeMind nor uv nor Homebrew."}
    bemind = Path(args.bemind_command)
    available = bemind.is_file() and os.access(bemind, os.X_OK)
    uv = getattr(args, "_uv_command", None) or d.shutil.which("uv")
    brew = homebrew_command() if not uv else None
    blockers = []
    if not available:
        blockers.append("Existing BeMind is required for install/upgrade; supply --bemind-command PATH. This frontend does not install BeMind.")
    if not uv:
        blockers.append("uv is missing: separate consent is required to install it using the displayed existing Homebrew executable." if brew else
                        "uv is missing and no existing Homebrew executable is available. Install uv with your approved package manager, put uv on PATH, then rerun setup. Homebrew itself will not be installed.")
    return {"required": True, "bemind_available": available, "uv": uv, "brew": brew, "blockers": blockers}


def preview(args, receipt, status):
    mode = d.deployment_mode(args, receipt)
    current = None
    if receipt and status == "installed":
        # Ownership/path validation has already passed. Read metadata, never import
        # the installed core or execute its CLI to discover a version.
        data = d.read_bytes(Path(receipt["runtimes"][-1]) / "pyproject.toml")
        if data:
            try:
                project = d.tomllib.loads(data.decode())["project"]
                if (project.get("name") == "ledger-memory" and isinstance(project.get("version"), str)
                        and d.re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", project["version"])):
                    current = project["version"]
            except (ValueError, TypeError, KeyError, UnicodeError):
                pass
    result = {"status": status, "action": args.command, "installer_version": INSTALLER_VERSION, "config_dir": args.config_dir,
              "current_version": current, "prerequisites": prerequisites(args),
              "app_root": args.app_root, "store": args.store,
              "config": receipt["config"] if receipt else str(d.config_state(Path(args.config_dir), None)[0]),
              "memory_mode": mode, "bemind_command": args.bemind_command,
              "preserve": "All existing memory, histories, backups, model caches, manual binaries and gateway write receipts."}
    if args.command == "uninstall":
        result.update(remove="Receipt-owned registration and managed configuration spans only" if args.keep_runtime else
                      "Receipt-owned registration, managed configuration spans and listed owned runtimes",
                      runtimes=[] if args.keep_runtime else receipt["runtimes"],
                      configuration_scope=[{"path": item["path"], "kind": item["kind"], "fields": item["managed"]}
                                           for item in receipt.get("manifest", [])] or
                                          [{"path": receipt["config"], "fields": ["mcp.ledger", "owned instructions reference", "owned ledger_* permission"]}],
                      agents=receipt.get("agents"),
                      retained_state=str(Path(args.config_dir) / "ledger-memory-deployment"),
                      embed_threads=receipt.get("embed_threads", 2),
                      embed_threads_policy=receipt.get("embed_threads_policy", "legacy core default"),
                      thread_activation=d.THREAD_RESTART_NOTE,
                      stop="Stop the owned Luna gateway if present; busy or unproved cleanup refuses deletion.")
    else:
        _, checksum, version = d.verified_archive(Path(args.archive).expanduser())
        result.update(version=version, archive=str(Path(args.archive).expanduser().absolute()), archive_sha256=checksum,
                      python=args.python, **args._thread_selection,
                      replace_instructions=args.replace_instruction or (receipt or {}).get("replace_instructions", []),
                      agents=({"path": args.agents_file, "section": args.agents_section} if args.agents_file else (receipt or {}).get("agents")),
                      memory_write_policy=args.memory_write or (receipt or {}).get("memory_write_policy", "ask"),
                      permissions="Existing stronger user policies win; Luna plan-agent writes remain denied. Direct mode retains its ledger_* policy.",
                      model=d.LUNA_MODEL if mode == "luna" else "Client-selected model (direct MCP)",
                      downloads="After confirmation: uv sync --frozen installs locked dependencies including semantic support; uv may download the requested Python.",
                       semantic=("OPT-IN: enable bge-m3 local embeddings; potentially multi-GB model download and local model execution. "
                                 + ("Import from " + str(Path(args.model_dir).expanduser().absolute())
                                    + "; no curl download. The unmodified core may try its urllib downloader if the verified cache disappears after the final check." if args.model_dir and args.curl_model else
                                    "Import from " + str(Path(args.model_dir).expanduser().absolute()) + "; missing artifacts may still require download." if args.model_dir else
                                    "After final approval, fetch approximately 2.29 GB of pinned BAAI/bge-m3 files from https://huggingface.co "
                                    + "at revision " + d.model_download.REVISION + " via curl only if the installed core cannot verify the cache. "
                                    + "HTTPS-only redirects; verify every size and SHA-256 before import. Each of 9 files has a 4-hour curl limit; "
                                    + "the whole install may take hours. Configured corporate HTTPS proxies are used; proxy credentials may be sent "
                                    + "to that proxy, but no memory content, authentication headers, or curl credentials go to Hugging Face. "
                                    + "No curl or semantic enable runs after a failed curl download. If the cache disappears after the final check, "
                                    + "the unmodified core's enable command may still try its urllib downloader. "
                                    + "Backlog indexing is scheduled asynchronously after enable." if args.curl_model else
                                    "Download required model artifacts unless already cached.")) if args.semantic else
                               "Not requested: no semantic-enable/model-download command. Existing semantic settings and models are preserved.",
                      thread_activation=d.THREAD_RESTART_NOTE,
                      restart="Restart BeMind after installation to load the managed configuration.")
    return result


def show_preview(plan):
    print(f"\nLedger Memory installer {plan['installer_version']}: {plan['action'].title()}")
    current = plan["current_version"] or ("unknown (runtime metadata unavailable)" if plan["status"] == "installed" else "none")
    print(f"Detected state: {plan['status']}; current installed core: {current}")
    if "version" in plan:
        print(f"Target core: {plan['version']}")
        if plan["current_version"] == plan["version"]:
            print("Same core version; a deployment/runtime upgrade is still available.")
        print(f"Archive: {plan['archive']}")
    policy = "automatic" if plan["embed_threads_policy"] == "auto" else plan["embed_threads_policy"]
    print(f"Mode: {plan['memory_mode']}; embedding threads: {plan['embed_threads']} ({policy})")
    print(f"Application: {plan['app_root']}\nMemory store: {plan['store']}\nConfiguration: {plan['config']}")
    if plan["action"] == "uninstall":
        print(f"Remove: {plan['remove']}")
        for item in plan["configuration_scope"]:
            fields = ", ".join(".".join(field) if isinstance(field, list) else field for field in item["fields"])
            print(f"Managed configuration: {item['path']} ({fields or item.get('kind', 'managed spans')})")
        for runtime in plan["runtimes"]:
            print(f"Remove owned runtime: {runtime}")
        print(f"Retained recovery state: {plan['retained_state']}")
        print("No memory/data deletion. " + plan["preserve"])
        print(plan["stop"])
    else:
        print(f"BeMind: {plan['bemind_command']}; runtime Python: {plan['python']}")
        print(f"Model: {plan['model']}")
        print(f"Dependency downloads: {plan['downloads']}")
        print(f"Model downloads: {plan['semantic']}")
        effect = ("register mcp.memory; deny raw ledger_*; memory_read defaults to allow; requested memory_write="
                  + plan["memory_write_policy"] + "; plan-agent writes denied") if plan["memory_mode"] == "luna" else (
                  "register mcp.ledger; add ledger_* allow only when existing permission policy permits")
        print("Managed configuration: " + effect + "; add managed memory rules reference.")
        print("Permissions: " + plan["permissions"])
        if plan["replace_instructions"]:
            print("Replace instruction references: " + ", ".join(plan["replace_instructions"]))
        print("Preserve: " + plan["preserve"])
        print(plan["restart"])
    if plan.get("agents"):
        print(f"Managed AGENTS section: {plan['agents']['path']} / {plan['agents']['section']}")
    print(plan["thread_activation"])
    for blocker in plan["prerequisites"]["blockers"]:
        print("Prerequisite: " + blocker)


def main(argv=None, *, input_fn=None):
    args = parser().parse_args(argv)
    prerequisite_started = False
    try:
        if sys.platform != "darwin" or sys.version_info < (3, 11):
            raise d.DeployError("Interactive setup requires macOS and Python 3.11 or newer.")
        args, receipt, status = inspect_installation(args)
        action = args.action
        if action is None and status == "installed" and not (args.dry_run or args.inspect_json):
            print("Managed installation found: 1. Upgrade  2. Uninstall  3. Cancel")
            choice = answer("Choose an action (Enter cancels): ", input_fn)
            action = {"1": "upgrade", "upgrade": "upgrade", "2": "uninstall", "uninstall": "uninstall"}.get(choice)
            if action is None:
                print("Cancelled; nothing changed.")
                return 0
        action = action or ("upgrade" if status == "installed" else "install")
        if action == "install" and status == "installed":
            action = "upgrade"
        if (action == "uninstall" and receipt is None) or (action == "upgrade" and status != "installed"):
            raise d.DeployError("Selected action does not match the inspected installation state.")
        args.command = "uninstall" if action == "uninstall" else "install"
        if args.auto_model and args.command == "install":
            args.semantic = True
            args.curl_model = True
        if args.command == "install":
            args._thread_selection = d.resolve_thread_selection(args.embed_threads, receipt)
        required = prerequisites(args)
        readonly = args.dry_run or args.inspect_json
        if args.command == "install" and not readonly and not required["bemind_available"]:
            raise d.DeployError(required["blockers"][0])
        # Validate the complete configuration/archive plan before offering any
        # prerequisite mutation. Only the read-only engine may defer prerequisites.
        preflight = argparse.Namespace(**vars(args))
        preflight.dry_run = True
        if args.command == "uninstall":
            preflight.keep_runtime = True
        d.deploy(preflight, check_prerequisites=False)
        if args.command == "install" and not readonly and not required["uv"]:
            if not required["brew"]:
                raise d.DeployError(required["blockers"][-1])
            if os.geteuid() == 0:
                raise d.DeployError("Run setup as your normal user. Homebrew prerequisite installation never uses sudo or root.")
            args._brew_command = required["brew"]
            before = material_state(args, receipt)
            print("\nInstall prerequisite only: uv")
            print("Command arguments: " + json.dumps([required["brew"], "install", "uv"]))
            print("Homebrew may download uv and its dependencies into its existing prefix/cache. "
                  "Auto-update, install cleanup and analytics are disabled; no sudo, no interactive child input.")
            print("This does not authorize Ledger installation. Approved prerequisite changes may remain after later cancellation or failure.")
            if answer("Approve this prerequisite only? Type yes or ok (anything else cancels): ", input_fn) not in ("yes", "ok"):
                print("Cancelled; nothing changed.")
                return 0
            checked, current_receipt, current_status = inspect_installation(args)
            if current_status != status or current_receipt != receipt or material_state(checked, current_receipt) != before:
                raise d.DeployError("Material state changed after prerequisite preview; nothing executed. Review a new preview.")
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("HOMEBREW_", "LEDGER_", "PYTHON", "UV_"))
                   and key not in ("BASH_ENV", "ENV", "SUDO_ASKPASS")}
            env.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_CLEANUP="1",
                       HOMEBREW_NO_ANALYTICS="1", NONINTERACTIVE="1")
            prerequisite_started = True
            try:
                result = d.run_process([required["brew"], "install", "uv"], cwd=Path.home(), env=env,
                                       stdin=subprocess.DEVNULL, stdout=None, stderr=None, check=True)
                if result.returncode:
                    raise d.DeployError("Homebrew returned a failure status.")
            except (d.DeployError, OSError, subprocess.SubprocessError):
                raise d.DeployError("uv prerequisite installation failed; Homebrew changes may remain. No Ledger deployment was executed. Review Homebrew before retrying.") from None
            uv = d.shutil.which("uv") or str(Path(required["brew"]).parent / "uv")
            if not Path(uv).is_file() or not os.access(uv, os.X_OK):
                raise d.DeployError("Homebrew completed but uv is not executable. Put the approved Homebrew uv executable on PATH and rerun setup.")
            args._uv_command = str(Path(uv).absolute())
            args, receipt, status = inspect_installation(args)
            if action == "upgrade" and status != "installed":
                raise d.DeployError("Installation state changed during prerequisite setup; review a new preview.")
            if action == "install" and status == "installed":
                action = "upgrade"
            if not prerequisites(args)["bemind_available"]:
                raise d.DeployError("BeMind disappeared during prerequisite setup; restore it before installing Ledger.")
            args._thread_selection = d.resolve_thread_selection(args.embed_threads, receipt)
            print("uv is installed. Ledger still requires the following separate confirmation.")
        before = material_state(args, receipt)
        preflight = argparse.Namespace(**vars(args))
        preflight.dry_run = True
        # Stop/idle verification belongs to execution. Taking existing shared-model
        # locks here would prevent an owned running gateway from being uninstalled.
        if args.command == "uninstall":
            preflight.keep_runtime = True
        d.deploy(preflight, check_prerequisites=not readonly)
        plan = preview(args, receipt, status)
        plan["action"] = action
        if args.inspect_json:
            print(json.dumps(plan, indent=2))
            return 0
        show_preview(plan)
        if args.dry_run:
            print("Dry run only; no files created, downloads, model calls or worker changes.")
            return 0
        if answer("Approve exactly this " + action + " scope? Type yes or ok (anything else cancels): ", input_fn) not in ("yes", "ok"):
            print("Cancelled; no Ledger deployment changes." if prerequisite_started else "Cancelled; nothing changed.")
            return 0
        checked, current_receipt, current_status = inspect_installation(args)
        if current_status != status or current_receipt != receipt or material_state(checked, current_receipt) != before:
            raise d.DeployError("Material filesystem/configuration/receipt state changed after preview; nothing executed. Review a new preview.")
        if args.command == "install" and args.curl_model:
            args._curl_model_approved = True
        previous_umask = os.umask(0o077)
        try:
            message = d.deploy(args)
        finally:
            os.umask(previous_umask)
        print(message)
        return 2 if "failed" in message else 0
    except d.DeployError as exc:
        if args.inspect_json:
            print(json.dumps({"status": getattr(exc, "status", "invalid"), "error": str(exc)}))
        else:
            print("Setup refused: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Setup interrupted; inspect any pending recovery marker before retrying.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Setup refused ({type(exc).__name__}); preserve state and review recovery. No error content was logged.", file=sys.stderr)
        return 1
    finally:
        if prerequisite_started:
            print("Approved uv/Homebrew prerequisite changes are retained, even if the later Ledger operation was cancelled or failed.")


if __name__ == "__main__":
    raise SystemExit(main())
