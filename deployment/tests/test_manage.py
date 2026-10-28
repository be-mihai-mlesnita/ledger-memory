"""Interactive setup exercises the real transaction engine with fake deployment tools."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_deploy import box, config, d, put_config, receipt, tree, uninstall
from test_deploy_gateway import luna, legacy
import manage


@pytest.fixture
def front(luna, monkeypatch):
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: 4)
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    monkeypatch.setattr(manage, "interactive_terminal", lambda: True)
    monkeypatch.setattr(manage, "homebrew_command", lambda: None)
    uv = luna.root / "fake uv"
    uv.write_text("synthetic uv")
    uv.chmod(0o700)
    return luna


def flags(front, *extra):
    return ["--archive", str(front.release), "--bemind-command", front.args.bemind_command, *extra]


def responses(monkeypatch, *answers):
    remaining = iter(answers)
    def prompt(message):
        answer = next(remaining)
        if answer is EOFError:
            raise EOFError()
        if callable(answer):
            return answer(message)
        return answer
    monkeypatch.setattr("builtins.input", prompt)


@pytest.mark.parametrize("answer", ["", "no", "n", "y", "cancel", "false", EOFError])
def test_fresh_never_treats_blank_no_or_eof_as_approval(front, monkeypatch, answer):
    before = tree(front.root)
    responses(monkeypatch, answer)
    assert manage.main(flags(front)) == 0
    assert tree(front.root) == before and not front.calls
    assert not front.state.exists() and not front.app.exists() and not front.store.exists()


@pytest.mark.parametrize("answer", ["yes", "YES", "Ok", "  oK  "])
def test_fresh_explicit_confirmation_runs_selected_install(front, monkeypatch, capsys, answer):
    def approve(message):
        output = capsys.readouterr().out
        assert not front.calls and not front.state.exists()
        for expected in ("0.3.0rc3", "Application:", "Memory store:", "Configuration:", "bemind-proxy/gpt-6-luna",
                         "downloads", "embedding threads: 4 (automatic)", "safe restart"):
            assert expected in output
        assert "install scope" in message
        return answer
    responses(monkeypatch, approve)
    assert manage.main(flags(front)) == 0
    assert receipt(front)["status"] == "installed" and receipt(front)["embed_threads"] == 4
    assert receipt(front)["memory_mode"] == "luna"


@pytest.mark.parametrize("answer", ["", "no", "yes", "3", "cancel", EOFError])
def test_installed_menu_never_defaults_to_an_action(front, monkeypatch, answer):
    d.deploy(front.args)
    front.calls.clear()
    before = tree(front.root)
    responses(monkeypatch, answer)
    assert manage.main(flags(front)) == 0
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("menu", ["1", "Upgrade", "2", "Uninstall"])
@pytest.mark.parametrize("approval", ["", "no", EOFError])
def test_selected_menu_action_still_requires_confirmation(front, monkeypatch, menu, approval):
    d.deploy(front.args)
    front.calls.clear()
    before = tree(front.root)
    responses(monkeypatch, menu, approval)
    assert manage.main(flags(front)) == 0
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("mode", ["direct", "luna"])
def test_upgrade_retains_mode_write_threads_and_custom_receipt_paths(front, monkeypatch, mode):
    front.args.app_root = str(front.home / "custom applications")
    front.args.store = str(front.home / "custom memory")
    front.args.memory_mode = mode
    front.args.memory_write = "allow"
    front.args.embed_threads = 7
    d.deploy(front.args)
    old = receipt(front)
    front.calls.clear()
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: pytest.fail("Preserved selection must not recalculate"))
    responses(monkeypatch, "1", "yes")
    assert manage.main(flags(front)) == 0
    new = receipt(front)
    assert new["app_root"] == old["app_root"] and new["store"] == old["store"]
    assert new["memory_mode"] == mode and new["embed_threads"] == 7 and new["embed_threads_policy"] == "explicit"
    if mode == "luna":
        assert new["memory_write_policy"] == "allow"
    assert len(new["runtimes"]) == 2 and new["owner"] == old["owner"]


def test_upgrade_explicit_overrides_and_auto_frozen_at_preview(front, monkeypatch):
    front.args.embed_threads = 7
    front.args.memory_write = "allow"
    d.deploy(front.args)
    front.calls.clear()
    def approve(message):
        assert "upgrade scope" in message and not front.calls
        monkeypatch.setattr(d, "automatic_embed_threads", lambda: pytest.fail("Preview count must be pinned"))
        return "ok"
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--action", "upgrade", "--embed-threads", "auto", "--memory-write", "ask")) == 0
    new = receipt(front)
    assert new["embed_threads"] == 4 and new["embed_threads_policy"] == "auto"
    assert new["memory_write_policy"] == "ask"


def test_upgrade_can_switch_mode_explicitly(front, monkeypatch):
    d.deploy(front.args)
    responses(monkeypatch, "upgrade", "yes")
    assert manage.main(flags(front, "--memory-mode", "direct", "--embed-threads", "3")) == 0
    assert receipt(front)["memory_mode"] == "direct"
    assert config(front)["mcp"]["ledger"]["environment"]["LEDGER_EMBED_THREADS"] == "3"


@pytest.mark.parametrize("keep", [False, True])
def test_uninstall_scope_and_data_preservation_without_archive_or_uv(front, monkeypatch, capsys, keep):
    d.deploy(front.args)
    saved = receipt(front)
    cache = d.worker_root()
    cache.mkdir(parents=True)
    (cache / "model.onnx").write_bytes(b"synthetic shared model")
    manual = front.home / "manual-ledger"
    manual.write_bytes(b"manual executable")
    memory_before = tree(front.store)
    backups_before = tree(front.state / "backups")
    front.calls.clear()
    front.release.unlink()
    monkeypatch.setattr(d.shutil, "which", lambda _: None)
    def approve(message):
        output = capsys.readouterr().out
        assert "uninstall scope" in message and "Managed configuration:" in output
        assert "No memory/data deletion" in output and "Retained recovery state:" in output
        assert not front.calls
        return "yes"
    responses(monkeypatch, "2", approve)
    assert manage.main(flags(front, *(["--keep-runtime"] if keep else []))) == 0
    assert receipt(front)["status"] == "uninstalled"
    for name, data in memory_before.items():
        assert (front.store / name).read_bytes() == data
    for name, data in backups_before.items():
        assert (front.state / "backups" / name).read_bytes() == data
    assert (cache / "model.onnx").read_bytes() == b"synthetic shared model"
    assert manual.read_bytes() == b"manual executable"
    assert all(Path(path).exists() is keep for path in saved["runtimes"])
    assert all("sync" not in call[0] for call in front.calls)


def test_uninstalled_receipt_reinstall_preserves_owner_store_and_selection(front, monkeypatch, capsys):
    front.args.embed_threads = 5
    d.deploy(front.args)
    uninstall(front)
    old = receipt(front)
    before = (front.store / "memory.db").read_bytes()
    front.calls.clear()
    responses(monkeypatch, "ok")
    assert manage.main(flags(front)) == 0
    assert "Detected state: uninstalled" in capsys.readouterr().out
    new = receipt(front)
    assert new["owner"] == old["owner"] and new["embed_threads"] == 5
    assert (front.store / "memory.db").read_bytes() == before


@pytest.mark.parametrize("flag", ["--dry-run", "--inspect-json"])
@pytest.mark.parametrize("state", ["fresh", "installed", "uninstalled"])
def test_read_only_flows_never_create_files_stop_or_install(front, monkeypatch, capsys, flag, state):
    if state != "fresh":
        d.deploy(front.args)
    if state == "uninstalled":
        uninstall(front)
    front.calls.clear()
    capsys.readouterr()
    before = tree(front.root)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Read-only mode must not prompt"))
    monkeypatch.setattr(d, "gateway_control", lambda *a: pytest.fail("Read-only mode cannot control workers"))
    monkeypatch.setattr(d, "build_runtime", lambda *a: pytest.fail("Read-only mode cannot install"))
    assert manage.main(flags(front, flag)) == 0
    assert tree(front.root) == before and not front.calls
    if flag == "--inspect-json":
        assert json.loads(capsys.readouterr().out)["status"] == state


@pytest.mark.parametrize("issue", ["invalid_receipt", "pending", "quarantine", "restore", "missing_runtime", "missing_component", "edited_settings"])
def test_invalid_and_recovery_states_refuse_without_commands(front, monkeypatch, capsys, issue):
    d.deploy(front.args)
    capsys.readouterr()
    saved = receipt(front)
    if issue == "invalid_receipt":
        (front.state / "receipt.json").write_text("not json")
    elif issue == "pending":
        (front.state / "pending.json").write_text("interrupted")
    elif issue == "quarantine":
        (Path(saved["gateway"]["state_dir"]) / "quarantine.json").write_text("{}")
    elif issue == "restore":
        (front.store / "restore-in-progress.json").write_text("{}")
    elif issue == "missing_runtime":
        runtime = Path(saved["runtimes"][-1])
        runtime.rename(runtime.with_name("moved-runtime"))
    elif issue == "missing_component":
        (Path(saved["runtimes"][-1]) / "gateway/ledger_backend.py").unlink()
    else:
        Path(saved["gateway"]["settings"]).write_text("{}")
    before = tree(front.root)
    front.calls.clear()
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Unsafe states must not prompt"))
    assert manage.main(flags(front, "--inspect-json")) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == ("recovery" if issue in ("pending", "quarantine", "restore") else "invalid")
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("kind", ["config.json", "bemind.json", "bemind.jsonc", "module", "app", "rules"])
def test_unmanaged_states_refuse_adoption_and_uninstall(front, monkeypatch, capsys, kind):
    if kind.endswith(".json") or kind.endswith(".jsonc"):
        put_config(front, json.dumps({"mcp": {"ledger": legacy(front)}}), kind)
    elif kind == "rules":
        front.state.mkdir(parents=True)
        (front.state / "MEMORY-RULES.md").write_text("manual rules")
    else:
        root = front.app if kind == "app" else Path(os.environ["XDG_DATA_HOME"]) / "ledger-memory"
        root.mkdir(parents=True)
        (root / "manual.py").write_text("manual runtime")
    before = tree(front.root)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Unmanaged state must not prompt"))
    for action in ("install", "uninstall"):
        assert manage.main(flags(front, "--action", action, "--inspect-json")) == 1
        value = json.loads(capsys.readouterr().out)
        assert value["status"] == "unmanaged" and "migration" in value["error"].lower()
    assert tree(front.root) == before and not front.calls


def test_ledger_command_alone_is_never_managed_ownership(front, monkeypatch):
    manual = front.home / "ledger"
    manual.write_text("manual")
    monkeypatch.setattr(d.shutil, "which", lambda name: str(manual) if name == "ledger" else str(front.root / "fake uv"))
    args, saved, status = manage.inspect_installation(manage.parser().parse_args(flags(front)))
    assert status == "fresh" and saved is None
    responses(monkeypatch, "no")
    assert manage.main(flags(front)) == 0
    assert manual.read_text() == "manual" and not front.calls


@pytest.mark.parametrize("option", ["--app-root", "--store"])
def test_conflicting_explicit_paths_refused(front, monkeypatch, option):
    d.deploy(front.args)
    front.calls.clear()
    before = tree(front.root)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Conflicting override must not prompt"))
    assert manage.main(flags(front, option, str(front.home / "other"))) == 1
    assert tree(front.root) == before and not front.calls


def test_fresh_custom_paths_and_arguments_remain_literal(front, monkeypatch):
    app = front.home / "app ; not-a-shell"
    store = front.home / "store $(not-a-command)"
    custom_config = front.home / "custom config"
    responses(monkeypatch, "yes")
    assert manage.main(flags(front, "--app-root", str(app), "--store", str(store),
                             "--config-dir", str(custom_config), "--embed-threads", "2")) == 0
    saved = json.loads((custom_config / "ledger-memory-deployment/receipt.json").read_text())
    assert saved["app_root"] == str(app) and saved["store"] == str(store) and saved["embed_threads"] == 2
    assert all(isinstance(command, list) for command, _, _ in front.calls)


@pytest.mark.parametrize("change", ["config", "receipt", "rules", "settings", "marker", "archive", "sidecar", "pending", "store", "bemind", "environment", "gateway_code"])
def test_material_changes_after_prompt_refuse_execution(front, monkeypatch, change):
    d.deploy(front.args)
    saved = receipt(front)
    front.calls.clear()
    def approve(message):
        if change == "config":
            path = front.config / "bemind.jsonc"
            path.write_text(d.jsonc_edit.set_value(path.read_text(), ["theme"], "changed after preview"))
        elif change == "receipt":
            (front.state / "receipt.json").write_bytes(d.encoded({**saved, "new_metadata": "changed"}))
        elif change == "rules":
            Path(saved["instruction"]).write_text("changed")
        elif change == "settings":
            Path(saved["gateway"]["settings"]).write_text("{}")
        elif change == "marker":
            (Path(saved["runtimes"][-1]) / d.MARKER).write_text("changed")
        elif change == "archive":
            front.release.write_bytes(front.release.read_bytes() + b"changed")
        elif change == "sidecar":
            front.release.with_suffix(".zip.sha256").write_text("0" * 64)
        elif change == "pending":
            (front.state / "pending.json").write_text("{}")
        elif change == "store":
            front.store.chmod(0o755)
        elif change == "bemind":
            Path(front.args.bemind_command).write_text("changed")
        elif change == "gateway_code":
            (Path(saved["runtimes"][-1]) / "gateway/memory_gateway.py").write_text("changed")
        else:
            monkeypatch.setenv("LEDGER_HOME", str(front.home / "changed"))
        return "yes"
    responses(monkeypatch, "1", approve)
    assert manage.main(flags(front)) == 1
    assert not front.calls


def test_missing_bemind_is_required_even_in_direct_mode(front, monkeypatch):
    Path(front.args.bemind_command).unlink()
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("No prompt without BeMind"))
    assert manage.main(flags(front, "--memory-mode", "direct")) == 1
    assert not front.calls and not front.state.exists()


@pytest.mark.parametrize("answer", ["no", "yes"])
def test_semantic_opt_in_shows_large_download_before_any_commands(front, monkeypatch, capsys, answer):
    def approve(message):
        assert not front.calls
        output = capsys.readouterr().out
        assert "multi-GB" in output and "bge-m3" in output and "local model execution" in output
        return answer
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--semantic")) == 0
    semantic = [command for command, _, _ in front.calls if "semantic" in command and "enable" in command]
    assert bool(semantic) is (answer == "yes")


@pytest.mark.parametrize("answer", ["no", "yes"])
def test_curl_model_requires_final_preview_and_approval(front, monkeypatch, capsys, answer):
    events = []
    monkeypatch.setattr(d.model_download, "pinned_manifest", lambda runtime: events.append("manifest") or {})
    monkeypatch.setattr(d.model_download, "cached_model", lambda runtime, env: events.append("cached") or True)
    def approve(message):
        output = capsys.readouterr().out
        assert "multi-GB" in output and "2.29 GB" in output and "https://huggingface.co" in output
        assert d.model_download.REVISION in output and not events and not front.calls
        for warning in ("BAAI/bge-m3", "HTTPS-only redirects", "proxy credentials", "4-hour", "may take hours",
                        "urllib downloader", "asynchronously"):
            assert warning in output
        return answer
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--semantic", "--curl-model")) == 0
    assert events == (["manifest", "cached", "cached"] if answer == "yes" else [])


@pytest.mark.parametrize("flag", ["--inspect-json", "--dry-run"])
def test_curl_model_readonly_does_not_inspect_cache_or_download(front, monkeypatch, flag):
    monkeypatch.setattr(d.model_download, "cached_model", lambda *a: pytest.fail("No cache verification in preview"))
    monkeypatch.setattr(d.model_download, "curl_bundle", lambda *a: pytest.fail("No download in preview"))
    assert manage.main(flags(front, "--semantic", "--curl-model", flag)) == 0
    assert not front.calls


def test_curl_model_without_semantic_refused_before_consent(front, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("No consent for invalid scope"))
    assert manage.main(flags(front, "--curl-model")) == 1
    assert not front.calls and not front.state.exists()


def test_curl_model_with_explicit_directory_never_downloads(front, monkeypatch, capsys):
    model = front.root / "local model"
    model.mkdir()
    monkeypatch.setattr(d.model_download, "curl_bundle", lambda *a: pytest.fail("Explicit model source must not curl"))
    monkeypatch.setattr(d.model_download, "cached_model", lambda *a: True)
    def approve(message):
        output = capsys.readouterr().out
        assert "no curl download" in output and "urllib downloader" in output
        return "yes"
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--semantic", "--curl-model", "--model-dir", str(model))) == 0
    assert any(call[0][-3:] == ["semantic", "import", str(model)] for call in front.calls)


@pytest.mark.parametrize("action", ["install", "upgrade", "uninstall"])
def test_auto_model_only_applies_to_confirmed_install_or_upgrade(front, monkeypatch, action, capsys):
    events = []
    monkeypatch.setattr(d.model_download, "pinned_manifest", lambda runtime: events.append("manifest") or {})
    def cached(runtime, env):
        events.append("cached")
        return action != "upgrade" or "downloaded" in events
    monkeypatch.setattr(d.model_download, "cached_model", cached)
    monkeypatch.setattr(d.model_download, "curl_bundle", lambda runtime, env, parent, manifest: events.append("downloaded"))
    if action == "uninstall":
        d.deploy(front.args)
        front.calls.clear()
        responses(monkeypatch, "2", "yes")
        assert manage.main(flags(front, "--auto-model")) == 0
        assert receipt(front)["status"] == "uninstalled"
        assert not events
    elif action == "upgrade":
        d.deploy(front.args)
        old_receipt = receipt(front)
        front.calls.clear()
        responses(monkeypatch, "1", "yes")
        assert manage.main(flags(front, "--auto-model")) == 0
        assert receipt(front)["owner"] == old_receipt["owner"]
        assert receipt(front)["store"] == old_receipt["store"]
        assert len(receipt(front)["runtimes"]) == len(old_receipt["runtimes"]) + 1
        assert events == ["manifest", "cached", "downloaded", "cached"]
        assert any(call[0][-3:] == ["enable", "--model", "bge-m3"] for call in front.calls)
    else:
        responses(monkeypatch, "yes")
        assert manage.main(flags(front, "--auto-model")) == 0
        assert events == ["manifest", "cached", "cached"]
    assert "installer 0.3.1" in capsys.readouterr().out


def test_auto_model_readonly_preview_displays_installer_and_model_scope(front, capsys):
    assert manage.main(flags(front, "--auto-model", "--inspect-json")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["installer_version"] == "0.3.1"
    assert "curl" in plan["semantic"] and "huggingface.co" in plan["semantic"]
    assert plan["version"] == "0.3.0rc3"  # Synthetic archive metadata is not relabeled.


def test_no_yes_bypass(front):
    with pytest.raises(SystemExit):
        manage.main(flags(front, "--yes"))
    assert not front.calls


def test_isolated_entrypoint_help_needs_no_dependencies_or_model(front):
    path = Path(manage.__file__).resolve()
    result = subprocess.run([sys.executable, "-I", "-B", str(path), "--help"],
                            env=dict(os.environ), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and "--embed-threads" in result.stdout and "--archive" in result.stdout
    assert not front.calls and not front.state.exists()


def test_uninstalled_retry_cleanup_never_reinstalls(front, monkeypatch):
    d.deploy(front.args)
    saved = receipt(front)
    uninstall(front, "--keep-runtime")
    front.calls.clear()
    responses(monkeypatch, "yes")
    assert manage.main(flags(front, "--action", "uninstall")) == 0
    assert all(not Path(path).exists() for path in saved["runtimes"])
    assert not front.calls and receipt(front)["status"] == "uninstalled"


def test_uninstalled_receipt_with_restored_manual_config_refuses_readoption(front, monkeypatch, capsys):
    put_config(front, json.dumps({"mcp": {"ledger": legacy(front)}}))
    front.args.migrate_existing_ledger = True
    d.deploy(front.args)
    uninstall(front)
    front.calls.clear()
    capsys.readouterr()
    before = tree(front.root)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("No implicit migration after uninstall"))
    assert manage.main(flags(front, "--inspect-json")) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unmanaged"
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("action", ["upgrade", "uninstall"])
def test_fresh_explicit_invalid_action_is_refused(front, monkeypatch, action):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Invalid action must not prompt"))
    assert manage.main(flags(front, "--action", action)) == 1
    assert not front.calls and not front.state.exists()


@pytest.mark.parametrize("orphan", ["state", "store"])
def test_orphan_state_or_nonempty_store_requires_recovery(front, monkeypatch, capsys, orphan):
    directory = front.state if orphan == "state" else front.store
    directory.mkdir(parents=True)
    (directory / "preserve-me").write_text("orphan artifact")
    before = tree(front.root)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Recovery must precede prompting"))
    assert manage.main(flags(front, "--inspect-json")) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "recovery"
    assert tree(front.root) == before and not front.calls


def test_uninstall_revalidates_after_confirmation(front, monkeypatch):
    d.deploy(front.args)
    saved = receipt(front)
    front.calls.clear()
    def approve(message):
        path = front.config / "bemind.jsonc"
        path.write_text(d.jsonc_edit.set_value(path.read_text(), ["theme"], "concurrent change"))
        return "yes"
    responses(monkeypatch, "uninstall", approve)
    assert manage.main(flags(front)) == 1
    assert not front.calls and receipt(front)["status"] == "installed"
    assert all(Path(path).is_dir() for path in saved["runtimes"])


@pytest.mark.parametrize("mode", ["direct", "luna"])
def test_removed_bemind_does_not_block_inspection_or_uninstall(front, monkeypatch, mode):
    front.args.memory_mode = mode
    d.deploy(front.args)
    Path(front.args.bemind_command).unlink()
    front.calls.clear()
    monkeypatch.setattr(d.shutil, "which", lambda name: pytest.fail("Uninstall must not look up prerequisites: " + name))
    monkeypatch.setattr(manage, "homebrew_command", lambda: pytest.fail("Uninstall must not need Homebrew"))
    _, saved, status = manage.inspect_installation(manage.parser().parse_args(flags(front)))
    assert status == "installed" and saved["memory_mode"] == mode
    responses(monkeypatch, "2", "yes")
    assert manage.main(flags(front)) == 0
    assert receipt(front)["status"] == "uninstalled"
    assert all("sync" not in call[0] for call in front.calls)


@pytest.fixture
def missing_uv(front, monkeypatch):
    prefix = front.root / "approved brew ; literal"
    prefix.mkdir()
    brew, uv = prefix / "brew", prefix / "uv"
    brew.write_text("synthetic Homebrew executable")
    brew.chmod(0o700)
    front.brew, front.uv, front.brew_calls = brew, uv, []
    monkeypatch.setattr(manage, "homebrew_command", lambda: str(brew))
    monkeypatch.setattr(d.shutil, "which", lambda name: str(uv) if name == "uv" and uv.exists() else None)
    original = front.fake_run
    def run(command, **kwargs):
        if command == [str(brew), "install", "uv"]:
            front.brew_calls.append((command, kwargs))
            uv.write_text("synthetic installed uv")
            uv.chmod(0o700)
            return SimpleNamespace(returncode=0)
        return original(command, **kwargs)
    monkeypatch.setattr(d, "run_process", run)
    return front


@pytest.mark.parametrize("approval", ["", "no", "y", EOFError])
def test_prerequisite_cancellation_changes_nothing(missing_uv, monkeypatch, approval):
    front = missing_uv
    before = tree(front.root)
    responses(monkeypatch, approval)
    assert manage.main(flags(front)) == 0
    assert tree(front.root) == before and not front.calls and not front.brew_calls


@pytest.mark.parametrize("approval", ["", "no", EOFError])
def test_approved_prerequisite_remains_after_later_cancellation(missing_uv, monkeypatch, capsys, approval):
    front = missing_uv
    responses(monkeypatch, "yes", approval)
    assert manage.main(flags(front)) == 0
    output = capsys.readouterr().out
    assert front.uv.is_file() and len(front.brew_calls) == 1 and not front.calls
    assert "changes are retained" in output and "no Ledger deployment changes" in output
    assert not front.state.exists() and not front.store.exists() and not front.app.exists()


def test_prerequisite_then_fresh_operation_confirmation(missing_uv, monkeypatch, capsys):
    front = missing_uv
    monkeypatch.setenv("HOMEBREW_NO_AUTO_UPDATE", "0")
    monkeypatch.setenv("HOMEBREW_NO_INSTALL_CLEANUP", "0")
    monkeypatch.setenv("HOMEBREW_BREW_FILE", "/unapproved/hook")
    monkeypatch.setenv("BASH_ENV", "/unapproved/hook")
    monkeypatch.setenv("SUDO_ASKPASS", "/unapproved/hook")
    before_env = dict(os.environ)
    def prerequisite(message):
        output = capsys.readouterr().out
        assert "prerequisite only" in message and json.dumps([str(front.brew), "install", "uv"]) in output
        assert "may remain" in output and "no sudo" in output
        assert not front.calls and not front.brew_calls
        return "ok"
    def final(message):
        output = capsys.readouterr().out
        assert front.uv.is_file() and len(front.brew_calls) == 1 and not front.calls
        assert "install scope" in message and "Target core: 0.3.0rc3" in output
        assert "Ledger still requires" in output
        return "yes"
    responses(monkeypatch, prerequisite, final)
    assert manage.main(flags(front)) == 0
    command, kwargs = front.brew_calls[0]
    assert command == [str(front.brew), "install", "uv"] and "sudo" not in command
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["check"] is True
    assert kwargs["env"]["HOMEBREW_NO_AUTO_UPDATE"] == kwargs["env"]["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"
    assert kwargs["env"]["HOMEBREW_NO_ANALYTICS"] == kwargs["env"]["NONINTERACTIVE"] == "1"
    assert not {"BASH_ENV", "SUDO_ASKPASS", "HOMEBREW_BREW_FILE"} & kwargs["env"].keys()
    assert dict(os.environ) == before_env
    assert receipt(front)["status"] == "installed" and front.calls[0][0][0] == str(front.uv)


def test_brew_bin_fallback_does_not_modify_parent_path(missing_uv, monkeypatch):
    front = missing_uv
    monkeypatch.setattr(d.shutil, "which", lambda _: None)
    original_path = os.environ.get("PATH")
    responses(monkeypatch, "yes", "yes")
    assert manage.main(flags(front)) == 0
    assert front.calls[0][0][0] == str(front.uv) and os.environ.get("PATH") == original_path


@pytest.mark.parametrize("flag", ["--dry-run", "--inspect-json"])
@pytest.mark.parametrize("brew_available", [False, True])
def test_readonly_missing_uv_reports_prerequisite_without_any_commands(missing_uv, monkeypatch, capsys, flag, brew_available):
    front = missing_uv
    if not brew_available:
        monkeypatch.setattr(manage, "homebrew_command", lambda: None)
    monkeypatch.setattr(d, "run_process", lambda *a, **k: pytest.fail("Read-only mode cannot run uv or brew"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Read-only mode must not prompt"))
    before = tree(front.root)
    assert manage.main(flags(front, flag)) == 0
    output = capsys.readouterr().out
    assert "uv is missing" in output and tree(front.root) == before
    if flag == "--inspect-json":
        value = json.loads(output)
        assert value["status"] == "fresh" and value["prerequisites"]["uv"] is None


def test_missing_brew_is_concrete_blocker_without_mutation(missing_uv, monkeypatch, capsys):
    front = missing_uv
    monkeypatch.setattr(manage, "homebrew_command", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("No executable to approve"))
    before = tree(front.root)
    assert manage.main(flags(front)) == 1
    assert "no existing Homebrew" in capsys.readouterr().err
    assert tree(front.root) == before and not front.brew_calls and not front.calls


@pytest.mark.parametrize("change", ["config", "brew", "archive"])
def test_tampering_after_prerequisite_preview_prevents_brew(missing_uv, monkeypatch, change):
    front = missing_uv
    def approve(message):
        if change == "config":
            put_config(front, '{"theme":"new"}')
        elif change == "brew":
            front.brew.write_text("different command")
        else:
            front.release.write_bytes(b"different archive")
        return "yes"
    responses(monkeypatch, approve)
    assert manage.main(flags(front)) == 1
    assert not front.brew_calls and not front.calls and not front.uv.exists()


def test_reinspect_after_prerequisite_failure_to_preserve_state(missing_uv, monkeypatch, capsys):
    front = missing_uv
    def corrupt(command, **kwargs):
        assert command == [str(front.brew), "install", "uv"]
        front.uv.write_text("installed")
        front.uv.chmod(0o700)
        front.state.mkdir(parents=True)
        (front.state / "pending.json").write_text("recovery required")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(d, "run_process", corrupt)
    responses(monkeypatch, "yes")
    assert manage.main(flags(front)) == 1
    output = capsys.readouterr()
    assert "Interrupted deployment" in output.err and "changes are retained" in output.out
    assert front.uv.exists() and not front.calls


def test_failed_prerequisite_does_not_install_ledger(missing_uv, monkeypatch, capsys):
    front = missing_uv
    monkeypatch.setattr(d, "run_process", lambda *a, **k: SimpleNamespace(returncode=3))
    responses(monkeypatch, "yes")
    assert manage.main(flags(front)) == 1
    output = capsys.readouterr()
    assert "prerequisite installation failed" in output.err and "changes are retained" in output.out
    assert not front.state.exists() and not front.calls


@pytest.mark.parametrize("phase", ["prerequisite", "install", "menu", "uninstall"])
def test_nonterminal_input_never_authorizes_mutation(missing_uv, monkeypatch, phase):
    front = missing_uv
    if phase != "prerequisite":
        front.uv.write_text("synthetic uv")
        front.uv.chmod(0o700)
    if phase in ("menu", "uninstall"):
        d.deploy(front.args)
        front.calls.clear()
    monkeypatch.setattr(manage, "interactive_terminal", lambda: False)
    before = tree(front.root)
    options = ["--action", "uninstall"] if phase == "uninstall" else []
    assert manage.main(flags(front, *options), input_fn=lambda _: pytest.fail("NonTTY must not consume piped yes")) == 0
    assert tree(front.root) == before and not front.calls and not front.brew_calls


@pytest.mark.parametrize("input_tty,output_tty", [(False, False), (False, True), (True, False), (True, True)])
def test_terminal_requires_visible_output_and_terminal_input(monkeypatch, input_tty, output_tty):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: output_tty)
    assert manage.interactive_terminal() is (input_tty and output_tty)
    called = []
    result = manage.answer("Test consent", lambda _: called.append(True) or "YES")
    assert result == ("yes" if input_tty and output_tty else "")
    assert bool(called) is (input_tty and output_tty)


def test_piped_yes_cannot_authorize_real_entrypoint(front):
    before = tree(front.root)
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
import manage
manage.d.shutil.which = lambda name: sys.argv[2] if name == "uv" else None
manage.homebrew_command = lambda: None
def forbidden(*args, **kwargs):
    raise AssertionError("piped consent ran a command")
manage.d.run_process = forbidden
raise SystemExit(manage.main(sys.argv[3:]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(Path(manage.__file__).parent),
                             str(front.root / "fake uv"), *flags(front)], input="yes\nyes\n", env=dict(os.environ),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and "piped input cannot authorize" in result.stdout
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("mode", ["direct", "luna"])
@pytest.mark.parametrize("action", ["upgrade", "uninstall"])
def test_same_database_index_progress_does_not_change_approval_scope(front, monkeypatch, mode, action):
    front.args.memory_mode = mode
    d.deploy(front.args)
    database = front.store / "memory.db"
    inode = database.stat().st_ino
    def approve(message):
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO facts VALUES (?)", ("newly indexed record" * 1000,))
        (front.store / "index-progress.json").write_text('{"completed":1}')
        (front.store / "index-progress.json").chmod(0o600)
        (front.store / "new-index-directory").mkdir(mode=0o700)
        assert database.stat().st_ino == inode
        return "yes"
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--action", action)) == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
    assert database.stat().st_ino == inode


@pytest.mark.parametrize("change", ["replacement", "permissions", "hardlink", "store_replacement", "module", "core_module"])
def test_database_structural_or_module_changes_still_refuse(front, monkeypatch, change):
    d.deploy(front.args)
    saved = receipt(front)
    front.calls.clear()
    database = front.store / "memory.db"
    def approve(message):
        if change == "replacement":
            old = database.read_bytes()
            database.rename(front.store / "old-memory.db")
            database.write_bytes(old)
            database.chmod(0o600)
        elif change == "permissions":
            database.chmod(0o644)
        elif change == "hardlink":
            os.link(database, front.store / "new-hardlink")
        elif change == "store_replacement":
            old = database.read_bytes()
            front.store.rename(front.store.with_name("old-store"))
            front.store.mkdir(mode=0o700)
            database.write_bytes(old)
            database.chmod(0o600)
        elif change == "core_module":
            (Path(saved["runtimes"][-1]) / "src/ledger_memory/locking.py").write_text("synthetic module changed")
        else:
            (Path(saved["runtimes"][-1]) / "pyproject.toml").write_text('[project]\nname="ledger-memory"\nversion="9.9"\n')
        return "yes"
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--action", "upgrade")) == 1
    assert not front.calls and receipt(front) == saved


@pytest.mark.parametrize("busy", [False, True])
def test_gateway_activity_is_checked_by_transaction_not_snapshot_jitter(front, monkeypatch, capsys, busy):
    d.deploy(front.args)
    saved = receipt(front)
    front.calls.clear()
    capsys.readouterr()
    private = Path(saved["gateway"]["state_dir"])
    control = d.gateway_control
    def quiesce(saved, action, store):
        if action == "stop" and busy:
            raise d.DeployError("Owned gateway is busy; cannot quiesce.")
        return control(saved, action, store)
    monkeypatch.setattr(d, "gateway_control", quiesce)
    def approve(message):
        (private / "active-request.json").write_text('{"new":"request"}')
        (private / "supervisor.json").write_text('{"running":true}')
        bytecode = Path(saved["runtimes"][-1]) / "gateway/__pycache__"
        bytecode.mkdir()
        (bytecode / "synthetic.pyc").write_bytes(b"ordinary runtime bytecode")
        request_id = "new-approved-write"
        data = {"schema": 1, "request_id": request_id, "operation": "remember", "state": "committed",
                "payload_hash": d.digest(b"synthetic")}
        (private / "receipts" / (d.digest(request_id.encode()) + ".json")).write_bytes(d.encoded(data))
        return "yes"
    responses(monkeypatch, approve)
    assert manage.main(flags(front, "--action", "upgrade")) == (1 if busy else 0)
    output = capsys.readouterr()
    assert "Material filesystem" not in output.err
    if busy:
        assert "busy" in output.err and receipt(front) == saved
        assert all(Path(path).exists() for path in saved["runtimes"])
    else:
        new = receipt(front)
        assert len(new["runtimes"]) == 2
        assert list((Path(new["gateway"]["state_dir"]) / "receipts").iterdir())


def test_readable_same_version_upgrade_and_machine_preview(front, capsys):
    front.args.embed_threads = 7
    d.deploy(front.args)
    capsys.readouterr()
    assert manage.main(flags(front, "--dry-run")) == 0
    output = capsys.readouterr().out
    assert "current installed core: 0.3.0rc3" in output and "Target core: 0.3.0rc3" in output
    assert "Same core version; a deployment/runtime upgrade is still available" in output
    assert "embedding threads: 7 (explicit)" in output and not output.lstrip().startswith("{")
    assert "memory_write=ask" in output and "plan-agent writes denied" in output
    assert manage.main(flags(front, "--inspect-json")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["current_version"] == plan["version"] == "0.3.0rc3"


def test_inspection_without_bemind_reports_install_blocker(front, capsys):
    Path(front.args.bemind_command).unlink()
    assert manage.main(flags(front, "--inspect-json")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not plan["prerequisites"]["bemind_available"]
    assert "Existing BeMind" in plan["prerequisites"]["blockers"][0]
    assert not front.calls and not front.state.exists()


def test_execution_cannot_bypass_prerequisite_validation(front):
    before = tree(front.root)
    with pytest.raises(d.DeployError, match="read-only"):
        d.deploy(front.args, check_prerequisites=False)
    assert tree(front.root) == before and not front.calls


def test_upgrade_missing_uv_retains_selection_after_prerequisite(missing_uv, monkeypatch):
    front = missing_uv
    front.uv.write_text("synthetic existing uv")
    front.uv.chmod(0o700)
    front.args.embed_threads = 7
    front.args.memory_write = "allow"
    d.deploy(front.args)
    front.uv.unlink()
    saved = receipt(front)
    front.calls.clear()
    responses(monkeypatch, "1", "yes", "yes")
    assert manage.main(flags(front)) == 0
    new = receipt(front)
    assert new["owner"] == saved["owner"] and new["memory_mode"] == "luna"
    assert new["embed_threads"] == 7 and new["memory_write_policy"] == "allow"
    assert len(front.brew_calls) == 1


def test_prerequisite_refuses_root_and_never_sudo(missing_uv, monkeypatch):
    front = missing_uv
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Root cannot approve Homebrew"))
    before = tree(front.root)
    assert manage.main(flags(front)) == 1
    assert tree(front.root) == before and not front.brew_calls and not front.calls


def test_invalid_install_scope_never_installs_prerequisite(missing_uv, monkeypatch):
    front = missing_uv
    front.release.with_suffix(".zip.sha256").write_text("0" * 64)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Invalid archive must precede prerequisite consent"))
    before = tree(front.root)
    assert manage.main(flags(front)) == 1
    assert tree(front.root) == before and not front.brew_calls and not front.calls


def test_review_invalid_saved_write_policy_is_rejected(front):
    d.deploy(front.args)
    saved = receipt(front)
    saved["memory_write_policy"] = "not-a-valid-policy"
    (front.state / "receipt.json").write_bytes(d.encoded(saved))
    with pytest.raises(d.DeployError, match="receipt"):
        d.load_receipt(front.state / "receipt.json")


def test_review_empty_public_luna_store_is_rejected_before_preview(front):
    front.store.mkdir(mode=0o755)
    front.store.chmod(0o755)
    with pytest.raises(manage.InspectionError, match="store"):
        manage.inspect_installation(manage.parser().parse_args(flags(front)))
    assert not front.calls and not front.state.exists()


def test_review_upgrade_reuses_saved_bemind_outside_path(front, monkeypatch):
    d.deploy(front.args)
    monkeypatch.setattr(d.shutil, "which", lambda name: str(front.root / "fake uv") if name == "uv" else None)
    args, _, _ = manage.inspect_installation(manage.parser().parse_args(["--archive", str(front.release)]))
    assert args.bemind_command == front.args.bemind_command
    front.calls.clear()
    responses(monkeypatch, "1", "yes")
    assert manage.main(["--archive", str(front.release)]) == 0
    settings = d.parse_config(Path(receipt(front)["gateway"]["settings"]).read_bytes())
    assert settings["bemind_command"] == [front.args.bemind_command]


@pytest.mark.parametrize("policy", ["not-a-valid-policy", "deny", "", None, True, 1, [], {}])
def test_invalid_saved_write_policy_refuses_preview_and_upgrade(front, monkeypatch, capsys, policy):
    d.deploy(front.args)
    saved = receipt(front)
    saved["memory_write_policy"] = policy
    (front.state / "receipt.json").write_bytes(d.encoded(saved))
    before = tree(front.root)
    front.calls.clear()
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Invalid receipt cannot reach approval"))
    assert manage.main(flags(front, "--inspect-json", "--memory-write", "allow")) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"
    with pytest.raises(d.DeployError, match="receipt"):
        d.deploy(front.args)
    assert tree(front.root) == before and not front.calls


@pytest.mark.parametrize("policy", [None, "ask", "allow"])
def test_saved_write_policy_valid_or_legacy_absent_upgrades(front, monkeypatch, policy):
    front.args.memory_write = policy
    d.deploy(front.args)
    saved = receipt(front)
    if policy is None:
        saved.pop("memory_write_policy")
        (front.state / "receipt.json").write_bytes(d.encoded(saved))
    assert d.load_receipt(front.state / "receipt.json") == saved
    responses(monkeypatch, "yes")
    assert manage.main(flags(front, "--action", "upgrade")) == 0
    assert receipt(front)["memory_write_policy"] == (policy or "ask")


@pytest.mark.parametrize("problem", ["empty_0755", "db_0644", "sidecar_0644", "directory_owner",
                                     "db_owner", "sidecar_owner", "store_symlink", "db_symlink",
                                     "sidecar_symlink", "sidecar_hardlink"])
def test_luna_privacy_refuses_before_prerequisites_or_any_deployment_mutation(missing_uv, monkeypatch, problem):
    front = missing_uv
    front.store.mkdir(mode=0o700)
    database = front.store / "memory.db"
    sidecar = front.store / "memory.db-wal"
    if problem != "empty_0755":
        database.write_bytes(b"metadata-only synthetic database")
        database.chmod(0o600)
        sidecar.write_bytes(b"metadata-only synthetic sidecar")
        sidecar.chmod(0o600)
    if problem == "empty_0755":
        front.store.chmod(0o755)
    elif problem in ("db_0644", "sidecar_0644"):
        (database if problem == "db_0644" else sidecar).chmod(0o644)
    elif problem in ("directory_owner", "db_owner", "sidecar_owner"):
        target = {"directory_owner": front.store, "db_owner": database, "sidecar_owner": sidecar}[problem]
        original = Path.lstat
        def foreign_owner(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == target:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info
        monkeypatch.setattr(Path, "lstat", foreign_owner)
    elif problem == "store_symlink":
        original = front.store.with_name("original-store")
        front.store.rename(original)
        front.store.symlink_to(original, target_is_directory=True)
    elif problem in ("db_symlink", "sidecar_symlink"):
        path = database if problem == "db_symlink" else sidecar
        path.unlink()
        path.symlink_to(front.home / "nonexistent-target")
    else:
        os.link(sidecar, front.store / "another-link")
    before = tree(front.root)
    modes = {path: path.lstat().st_mode for path in (front.store, database, sidecar) if path.exists() or path.is_symlink()}
    monkeypatch.setattr(Path, "chmod", lambda *a, **kw: pytest.fail("Preflight must not repair permissions"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Store privacy must pass before prerequisite consent"))
    assert manage.main(flags(front)) == 1
    for dry_run in (True, False):
        front.args.dry_run = dry_run
        with pytest.raises(d.DeployError):
            d.deploy(front.args)
    assert not front.calls and not front.brew_calls
    assert not front.state.exists() and not front.app.exists()
    assert tree(front.root) == before
    assert all(path.lstat().st_mode == mode for path, mode in modes.items())


@pytest.mark.parametrize("initialized", [False, True])
def test_direct_mode_keeps_existing_public_store_permission_behavior(front, monkeypatch, initialized):
    front.store.mkdir(mode=0o755)
    front.store.chmod(0o755)
    files = [front.store]
    if initialized:
        database = front.store / "memory.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE synthetic (value TEXT)")
        database.chmod(0o644)
        files.append(database)
    modes = {path: path.stat().st_mode for path in files}
    responses(monkeypatch, "yes")
    assert manage.main(flags(front, "--memory-mode", "direct")) == 0
    assert all(path.stat().st_mode == mode for path, mode in modes.items())
    assert receipt(front)["memory_mode"] == "direct"


@pytest.mark.parametrize("state", ["missing", "empty", "initialized"])
def test_shared_luna_store_checker_is_metadata_only_and_nonmutating(front, monkeypatch, state):
    from gateway import memory_gateway as checker
    if state != "missing":
        front.store.mkdir(mode=0o700)
    if state == "initialized":
        (front.store / "memory.db").touch(mode=0o600)
        (front.store / "memory.db-wal").touch(mode=0o600)
        (front.store / ".ledger.sessions.lock").touch(mode=0o600)
    before = tree(front.root)
    before_env = dict(os.environ)
    imported_core = {name for name in sys.modules if name.startswith("ledger_memory")}
    read_bytes = Path.read_bytes
    def no_store_content(path):
        if path.is_relative_to(front.store):
            pytest.fail("Privacy preflight may only inspect store metadata")
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", no_store_content)
    monkeypatch.setattr(Path, "chmod", lambda *a, **k: pytest.fail("No chmod during preflight"))
    monkeypatch.setattr(checker, "private_dir", lambda *a: pytest.fail("No directory creation"))
    monkeypatch.setattr(checker, "load", lambda *a: pytest.fail("No gateway state loading"))
    monkeypatch.setattr(d, "run_process", lambda *a, **k: pytest.fail("No subprocess during preflight"))
    checker.store_preflight(front.store, allow_uninitialized=True)
    d.luna_store_preflight(front.store)
    assert dict(os.environ) == before_env
    assert {name for name in sys.modules if name.startswith("ledger_memory")} == imported_core
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert tree(front.root) == before and (front.store.exists() is (state != "missing"))


@pytest.mark.parametrize("override", [False, True])
def test_saved_bemind_precedes_path_but_explicit_override_wins(front, monkeypatch, override):
    d.deploy(front.args)
    alternate = front.root / "alternate bemind"
    alternate.write_text("synthetic alternate command")
    alternate.chmod(0o700)
    monkeypatch.setattr(d.shutil, "which", lambda name: str(front.root / "fake uv") if name == "uv" else str(alternate))
    options = ["--archive", str(front.release), "--action", "upgrade"]
    if override:
        options += ["--bemind-command", str(alternate)]
    responses(monkeypatch, "yes")
    assert manage.main(options) == 0
    settings = d.parse_config(Path(receipt(front)["gateway"]["settings"]).read_bytes())
    assert settings["bemind_command"] == [str(alternate) if override else front.args.bemind_command]


def test_missing_saved_bemind_does_not_silently_switch_or_block_uninstall(front, monkeypatch):
    d.deploy(front.args)
    Path(front.args.bemind_command).unlink()
    front.calls.clear()
    monkeypatch.setattr(d.shutil, "which", lambda *a: pytest.fail("Validated saved command precedes discovery"))
    options = ["--archive", str(front.release)]
    args, _, _ = manage.inspect_installation(manage.parser().parse_args(options))
    assert args.bemind_command == front.args.bemind_command
    responses(monkeypatch, "2", "yes")
    assert manage.main(options) == 0
    assert receipt(front)["status"] == "uninstalled"


@pytest.mark.parametrize("command", [[], ["relative/bemind"], [True], "not-a-list", ["/synthetic/bemind\u0000"]])
def test_saved_bemind_command_is_validated_before_reuse(front, monkeypatch, command):
    d.deploy(front.args)
    saved = receipt(front)
    path = Path(saved["gateway"]["settings"])
    settings = d.parse_config(path.read_bytes())
    settings["bemind_command"] = command
    path.write_bytes(d.encoded(settings))
    saved["gateway"]["settings_hash"] = d.digest(path.read_bytes())
    (front.state / "receipt.json").write_bytes(d.encoded(saved))
    front.calls.clear()
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Malformed saved command must not be approved"))
    assert manage.main(["--archive", str(front.release), "--inspect-json"]) == 1
    assert not front.calls
