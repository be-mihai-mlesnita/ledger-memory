"""Thread policy tests use synthetic homes, fake deployment tools and fake Ledger APIs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from test_deploy import box, config, d, put_config, receipt, tree
from test_deploy_gateway import luna
from test_gateway_backend import setup, scope_for, backend, contract, FakeLedger
from test_gateway_controller import gateway, layout


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "isolated-home"
    home.mkdir()
    for key in list(os.environ):
        if key.upper().startswith(("LEDGER_", "XDG_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    for suffix in ("CONFIG", "CACHE", "DATA", "STATE"):
        monkeypatch.setenv("XDG_" + suffix + "_HOME", str(home / suffix.lower()))


@pytest.mark.parametrize("count,expected", [(1, 1), (2, 1), (3, 1), (8, 2), (12, 4), (96, 32), (192, 32)])
def test_usable_logical_cpu_policy(monkeypatch, count, expected):
    monkeypatch.setattr(d.os, "process_cpu_count", lambda: count, raising=False)
    monkeypatch.setattr(d.os, "sched_getaffinity", lambda _: pytest.fail("Preferred API must win"), raising=False)
    monkeypatch.setattr(d.os, "cpu_count", lambda: pytest.fail("Preferred API must win"))
    assert d.automatic_embed_threads() == expected


@pytest.mark.parametrize("invalid", [None, 0, -1, True, False, "12", 12.0])
def test_invalid_preferred_api_falls_back_to_affinity(monkeypatch, invalid):
    monkeypatch.setattr(d.os, "process_cpu_count", lambda: invalid, raising=False)
    monkeypatch.setattr(d.os, "sched_getaffinity", lambda pid: set(range(12)) if pid == 0 else set(), raising=False)
    monkeypatch.setattr(d.os, "cpu_count", lambda: 192)
    assert d.automatic_embed_threads() == 4


@pytest.mark.parametrize("invalid", [None, set(), True, "123456", {-1, 0}, {True}, 12])
def test_invalid_affinity_falls_back_to_cpu_count(monkeypatch, invalid):
    monkeypatch.setattr(d.os, "process_cpu_count", None, raising=False)
    monkeypatch.setattr(d.os, "sched_getaffinity", lambda _: invalid, raising=False)
    monkeypatch.setattr(d.os, "cpu_count", lambda: 24)
    assert d.automatic_embed_threads() == 8


@pytest.mark.parametrize("error", [OSError, NotImplementedError, TypeError, ValueError])
def test_api_errors_and_missing_functions_fall_back(monkeypatch, error):
    def fail(*args):
        raise error()
    monkeypatch.setattr(d.os, "process_cpu_count", fail, raising=False)
    monkeypatch.setattr(d.os, "sched_getaffinity", fail, raising=False)
    monkeypatch.setattr(d.os, "cpu_count", lambda: 3)
    assert d.automatic_embed_threads() == 1
    monkeypatch.delattr(d.os, "process_cpu_count")
    monkeypatch.delattr(d.os, "sched_getaffinity")
    monkeypatch.setattr(d.os, "cpu_count", fail)
    assert d.automatic_embed_threads() == 1


@pytest.mark.parametrize("invalid", [None, True, False, 0, -4, "9", 9.1])
def test_last_cpu_api_invalid_defaults_to_one(monkeypatch, invalid):
    monkeypatch.setattr(d.os, "process_cpu_count", None, raising=False)
    monkeypatch.setattr(d.os, "sched_getaffinity", None, raising=False)
    monkeypatch.setattr(d.os, "cpu_count", lambda: invalid)
    assert d.automatic_embed_threads() == 1


@pytest.mark.parametrize("value", [True, False, 0, 33, -1, 2.5, "2.0", "True", "AUTO", "", "-1", None])
def test_selection_rejects_non_integer_and_out_of_range(value):
    with pytest.raises(d.argparse.ArgumentTypeError):
        d.embed_threads(value)


@pytest.mark.parametrize("value,expected", [(1, 1), (32, 32), ("1", 1), ("32", 32), ("auto", "auto")])
def test_selection_accepts_explicit_or_auto(value, expected):
    assert d.embed_threads(value) == expected


@pytest.mark.parametrize("value", ["0", "33", "true", "1.0"])
def test_cli_rejects_invalid_selection(value):
    with pytest.raises(SystemExit):
        d.parser().parse_args(["install", "--embed-threads", value])


@pytest.mark.parametrize("mode", ["direct", "luna"])
@pytest.mark.parametrize("selection", [None, "auto", 7])
def test_install_both_modes_and_upgrade_selection(luna, monkeypatch, mode, selection):
    luna.args.memory_mode = mode
    luna.args.embed_threads = selection
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: 4)
    monkeypatch.setenv("LEDGER_EMBED_THREADS", "31")
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "untrusted")
    before_env = dict(os.environ)
    result = d.deploy(luna.args)
    saved = receipt(luna)
    expected = 7 if selection == 7 else 4
    assert saved["embed_threads"] == expected
    assert saved["embed_threads_policy"] == ("explicit" if selection == 7 else "auto")
    if mode == "luna":
        settings = json.loads(Path(saved["gateway"]["settings"]).read_text())
        assert settings["embed_threads"] == expected and settings["embed_threads_policy"] == saved["embed_threads_policy"]
    else:
        assert config(luna)["mcp"]["ledger"]["environment"]["LEDGER_EMBED_THREADS"] == str(expected)
    for command, _, env in luna.calls:
        if "--settings" not in command:
            assert env["LEDGER_EMBED_THREADS"] == str(expected)
        assert "LEDGER_EMBEDDING_BACKEND" not in env
    assert "until a safe restart" in result and "No shared model worker was force-stopped or retuned" in result
    assert dict(os.environ) == before_env
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: 12)
    luna.args.embed_threads = None
    d.deploy(luna.args)
    assert receipt(luna)["embed_threads"] == expected
    luna.args.embed_threads = "auto"
    d.deploy(luna.args)
    assert receipt(luna)["embed_threads"] == 12 and receipt(luna)["embed_threads_policy"] == "auto"
    luna.args.embed_threads = 3
    d.deploy(luna.args)
    assert receipt(luna)["embed_threads"] == 3 and receipt(luna)["embed_threads_policy"] == "explicit"


@pytest.mark.parametrize("mode", ["direct", "luna"])
def test_legacy_receipt_stays_valid_until_actual_upgrade(luna, monkeypatch, mode):
    luna.args.memory_mode = mode
    d.deploy(luna.args)
    saved = receipt(luna)
    for key in ("embed_threads", "embed_threads_policy"):
        saved.pop(key)
    if mode == "luna":
        path = Path(saved["gateway"]["settings"])
        value = json.loads(path.read_text())
        value.pop("embed_threads")
        value.pop("embed_threads_policy")
        path.write_bytes(d.encoded(value))
        saved["gateway"]["settings_hash"] = d.digest(path.read_bytes())
    else:
        saved["entry"]["environment"].pop("LEDGER_EMBED_THREADS")
        value = config(luna)
        value["mcp"]["ledger"] = saved["entry"]
        put_config(luna, json.dumps(value))
    (luna.state / "receipt.json").write_bytes(d.encoded(saved))
    assert d.load_receipt(luna.state / "receipt.json") == saved
    before = tree(luna.root)
    luna.args.dry_run = True
    d.deploy(luna.args)
    assert tree(luna.root) == before
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: 9)
    luna.args.dry_run = False
    d.deploy(luna.args)
    assert receipt(luna)["embed_threads"] == 9


@pytest.mark.parametrize("selection", [
    {"embed_threads": True, "embed_threads_policy": "auto"},
    {"embed_threads": "2", "embed_threads_policy": "explicit"},
    {"embed_threads": 0, "embed_threads_policy": "auto"},
    {"embed_threads": 33, "embed_threads_policy": "auto"},
    {"embed_threads": 4}, {"embed_threads_policy": "auto"},
    {"embed_threads": 4, "embed_threads_policy": "hardware"},
])
def test_receipt_and_both_settings_validators_reject_bad_counts(luna, setup, selection):
    d.deploy(luna.args)
    saved = receipt(luna)
    saved.pop("embed_threads")
    saved.pop("embed_threads_policy")
    (luna.state / "receipt.json").write_bytes(d.encoded({**saved, **selection}))
    with pytest.raises(d.DeployError, match="receipt"):
        d.load_receipt(luna.state / "receipt.json")
    instance, _, _, cfg = setup
    contract.save_json(instance.settings, {**cfg, **selection})
    with pytest.raises(ValueError, match="invalid_settings"):
        instance.config()
    with pytest.raises(gateway.GatewayError, match="invalid_settings"):
        gateway.settings(instance.settings)


@pytest.mark.parametrize("selection", [{}, {"embed_threads": 6, "embed_threads_policy": "auto"},
                                      {"embed_threads": 32, "embed_threads_policy": "explicit"}])
def test_backend_count_is_set_before_factory_and_lazy_api_then_restored(setup, monkeypatch, selection):
    instance, _, _, cfg = setup
    cfg.update(selection)
    contract.save_json(instance.settings, cfg)
    assert gateway.settings(instance.settings).get("embed_threads") == selection.get("embed_threads")
    scope_for(setup)
    observed = []
    expected = str(selection["embed_threads"]) if selection else None
    class Ledger(FakeLedger):
        @contextmanager
        def session(self):
            observed.append(os.environ.get("LEDGER_EMBED_THREADS"))
            yield self
        def brief(self, **args):
            observed.append(os.environ.get("LEDGER_EMBED_THREADS"))
            return super().brief(**args)
    def factory(*args):
        observed.append(os.environ.get("LEDGER_EMBED_THREADS"))
        return Ledger()
    monkeypatch.setattr(backend, "ledger_factory", factory)
    monkeypatch.setattr(d, "automatic_embed_threads", lambda: pytest.fail("Runtime must not recalculate hardware"))
    before = dict(os.environ)
    instance.call("brief", {})
    assert observed == [expected] * 3
    assert dict(os.environ) == before


@pytest.mark.parametrize("key,value,selected,allowed", [
    ("LEDGER_EMBED_THREADS", "4", True, True),
    ("LEDGER_EMBED_THREADS", "8", True, False),
    ("LEDGER_EMBED_THREADS", "4", False, False),
    ("LEDGER_EMBED_THREADS", "04", True, False),
    ("LEDGER_HOME", "/other", True, False),
    ("LEDGER_EMBEDDING_BACKEND", "onnx", True, False),
    ("LEDGER_EMBED_NICE", "19", True, False),
    ("ledger_embed_threads", "4", True, False),
])
def test_raw_environment_denied_except_matching_stored_count(setup, monkeypatch, key, value, selected, allowed):
    instance, _, _, cfg = setup
    if selected:
        cfg.update(embed_threads=4, embed_threads_policy="explicit")
        contract.save_json(instance.settings, cfg)
    scope_for(setup)
    calls = []
    monkeypatch.setattr(backend, "ledger_factory", lambda *args: calls.append(args) or FakeLedger())
    monkeypatch.setenv(key, value)
    if allowed:
        instance.call("brief", {})
        assert len(calls) == 1 and os.environ[key] == value
    else:
        with pytest.raises(ValueError, match="unsafe_ledger_environment"):
            instance.call("brief", {})
        assert not calls


def test_environment_restored_after_core_failure(setup, monkeypatch):
    instance, _, _, cfg = setup
    cfg.update(embed_threads=4, embed_threads_policy="auto")
    contract.save_json(instance.settings, cfg)
    scope_for(setup)
    def fail(*args):
        assert os.environ["LEDGER_EMBED_THREADS"] == "4"
        raise RuntimeError("synthetic")
    monkeypatch.setattr(backend, "ledger_factory", fail)
    before = dict(os.environ)
    with pytest.raises(ValueError, match="backend_read_failed"):
        instance.call("brief", {})
    assert dict(os.environ) == before


def test_gateway_worker_drops_raw_ledger_env_and_keeps_settings_contract(layout, monkeypatch):
    cfg = json.loads(layout.path.read_text())
    cfg.update(embed_threads=4, embed_threads_policy="auto")
    gateway.save(layout.path, cfg)
    layout = gateway.Layout(layout.path)
    monkeypatch.setenv("LEDGER_EMBED_THREADS", "31")
    monkeypatch.setenv("LEDGER_EMBED_NICE", "19")
    class StopSpawn(Exception):
        pass
    def capture(command, **kwargs):
        assert "LEDGER_EMBED_THREADS" not in kwargs["env"] and "LEDGER_EMBED_NICE" not in kwargs["env"]
        assert "preexec_fn" not in kwargs and "nice" not in command
        raise StopSpawn()
    monkeypatch.setattr(gateway.subprocess, "Popen", capture)
    with pytest.raises(StopSpawn):
        gateway.Worker(layout, None).start()
    private = json.loads((layout.state / "config/bemind/bemind.jsonc").read_text())
    assert private["mcp"]["ledger"]["command"][-2:] == ["--settings", str(layout.path)]
    assert gateway.settings(layout.path)["embed_threads"] == 4


def test_no_niceness_or_performance_core_policy_added():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "bemind_deploy.py", root / "manage.py", root / "gateway/ledger_backend.py"):
        source = path.read_text()
        assert "os.nice(" not in source and "setpriority(" not in source
        assert "hw.perflevel" not in source and "LEDGER_EMBED_NICE" not in source
